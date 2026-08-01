// Read the Electron fuse wire back out of a packaged Hearth.exe and fail if
// it is not what package.json's "electronFuses" block asked for.
//
//     node verify-fuses.js ../../build/dist/win-unpacked/Hearth.exe
//
// Why this is a build step and not a comment in a config file. Electron's
// fuses are flipped by rewriting a sentinel inside the packaged executable,
// after electron-builder has already copied the runtime into place. That is
// several layers away from the JSON that requests it: a key typo, an
// electron-builder upgrade that renames an option, a packaging path that
// skips the step, all leave a build that looks completely normal and ships a
// binary with every fuse still at its permissive default. Nothing else in the
// build would notice.
//
// What is actually at stake, stated plainly because it is not obvious from
// the option names. With RunAsNode enabled, the shipped executable runs
// arbitrary JavaScript handed to it through an environment variable:
//
//     ELECTRON_RUN_AS_NODE=1 Hearth.exe -e "<anything>"
//
// Demonstrated against this project's own build. On an unsigned binary that
// is not privilege escalation, since whoever sets that variable is already
// the user. The moment Hearth is code-signed it becomes something else: a
// signed, reputable-looking process that third-party malware can borrow to
// execute its own code, which launders that malware past reputation checks
// and endpoint detection at Hearth's users' expense and at the expense of
// people who never installed Hearth at all. Signing a binary in this state
// would do harm outside this project, so the check is a hard failure rather
// than a warning, and it fires long before any certificate is involved.
//
// The three Node-execution fuses close that door. onlyLoadAppFromAsar and
// enableEmbeddedAsarIntegrityValidation close the neighbouring one: without
// them the signed executable will happily load application code from a
// loose directory next to it, or from a modified asar, so an attacker with
// write access to the install directory inherits the signature without
// touching the binary.
//
// grantFileProtocolExtraPrivileges is the last one, and it is here because it
// costs this app nothing. Enabled, it hands file:// pages the privileges of a
// normal web origin: a local HTML file can then be treated as same-origin with
// other local files and reach them with fetch and XMLHttpRequest. Hearth never
// loads a file:// page at all -- main.js serves the UI from a loopback HTTP
// origin and refuses to navigate anywhere else (see origin.js for why the
// sidecar's own Host and Origin checks force that) -- so turning the privileges
// off removes a capability the product does not use. It was verified by
// building with the fuse disabled and running a full turn against the packaged
// binary, not by reasoning about it alone.

const path = require("path");
const { getCurrentFuseWire, FuseV1Options } = require("@electron/fuses");

//: name -> the value this build requires. Kept here rather than read back
//: out of package.json on purpose: a check that derives its expectations
//: from the same file it is checking passes no matter what that file says.
const REQUIRED = {
  RunAsNode: false,
  EnableNodeOptionsEnvironmentVariable: false,
  EnableNodeCliInspectArguments: false,
  OnlyLoadAppFromAsar: true,
  EnableEmbeddedAsarIntegrityValidation: true,
  EnableCookieEncryption: true,
  GrantFileProtocolExtraPrivileges: false,
};

// getCurrentFuseWire reports each fuse as the character that sits in the
// binary, not as a boolean: "1" enabled, "0" disabled, "r" removed. Older
// versions handed back the char CODES (49 and 48) instead, so accept both
// rather than silently reading every fuse as "unknown" after an upgrade.
function readState(raw) {
  if (raw === true || raw === "1" || raw === 1 || raw === 49) return true;
  if (raw === false || raw === "0" || raw === 0 || raw === 48) return false;
  return null;
}

async function main() {
  const exe = process.argv[2];
  if (!exe) {
    console.error("usage: node verify-fuses.js <path to Hearth.exe>");
    process.exit(2);
  }
  const wire = await getCurrentFuseWire(exe);
  const problems = [];
  const rows = [];
  for (const [name, want] of Object.entries(REQUIRED)) {
    const state = readState(wire[FuseV1Options[name]]);
    rows.push(
      "  " +
        name.padEnd(38) +
        (state === null ? "UNREADABLE" : state ? "ENABLED" : "disabled")
    );
    if (state !== want) {
      problems.push(
        name +
          ": expected " +
          (want ? "ENABLED" : "disabled") +
          ", found " +
          (state === null ? "an unreadable value" : state ? "ENABLED" : "disabled")
      );
    }
  }
  console.log("  fuse wire in " + path.basename(exe) + ":");
  console.log(rows.join("\n"));
  if (problems.length) {
    console.error(
      "\nthe packaged binary's fuses are not what this build requires:\n  " +
        problems.join("\n  ") +
        "\n\nThis binary runs arbitrary JavaScript from an environment " +
        "variable, or loads application code from outside its asar. It must " +
        "not be shipped and must not be signed."
    );
    process.exit(1);
  }
}

main().catch((err) => {
  console.error("could not read the fuse wire: " + (err && err.stack ? err.stack : err));
  process.exit(1);
});
