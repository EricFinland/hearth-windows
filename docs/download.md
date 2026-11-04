# Download and install Hearth

This page is for anyone who wants to **use** Hearth, not build it. No
programming tools, no terminal, no Python. If you can download and run a
normal Windows program, you can do this.

It takes about five minutes, plus however long your chosen model takes to
download.

## Before you start: Windows will warn you, and that is expected

Hearth is free, open-source software. Every line of its code is public in
[this repository](https://github.com/EricFinland/hearth-windows), and every
installer is built by GitHub's own servers from that public code, not on
anyone's personal computer.

What Hearth does **not** have is a paid code-signing certificate. Windows uses
those certificates to recognise who published a program, and when a program
has none, Windows says the publisher is "unknown" and shows a warning. That
warning is about the missing certificate. It is not Windows saying it found
anything wrong with Hearth.

So you will see a couple of warnings along the way. This page shows you
exactly what each one looks like and which button to press. If you see a
warning that is **not** described on this page, stop and
[open an issue](https://github.com/EricFinland/hearth-windows/issues) instead
of clicking through it.

## What you need

- **Windows 10 or Windows 11.**
- **About 8 GB of free memory (RAM)** for a small model. More lets you run
  bigger, smarter models. Hearth checks your computer and tells you which
  models fit, so you do not need to work this out yourself.
- **About 10 GB of free disk space** for the app and one model.
- **A graphics card helps but is not required.** Without one, models still
  run, just more slowly.

You do **not** need administrator rights, Python, Ollama, or anything else.
Hearth brings everything it needs with it.

## Step 1: download the installer

1. Go to the **[latest release page](https://github.com/EricFinland/hearth-windows/releases/latest)**.
2. Scroll down to the section called **Assets**.
3. Click the file named **`Hearth-Setup-<version>.exe`** (for example
   `Hearth-Setup-0.1.0.exe`). Do not download the "Source code" files; those
   are for programmers.

### If your browser warns you about the download

Because Hearth is new and not downloaded by millions of people yet, your
browser may say something like *"Hearth-Setup-0.1.0.exe isn't commonly
downloaded"*.

**Microsoft Edge:**

1. Hover over the download in the downloads panel and click the **`...`**
   (three dots) button.
2. Click **Keep**.
3. If it asks again, click **Show more**, then **Keep anyway**.

**Google Chrome:**

1. In the downloads list, click the download's arrow or **`⋮`** menu.
2. Click **Keep** (or **Download suspicious file**, then **Keep**).

**Firefox** usually downloads it without asking.

## Step 2 (optional, recommended): check the file is genuine

This step proves the file you downloaded is exactly the one GitHub built,
byte for byte, and was not swapped or damaged on the way. You can skip it,
but it takes thirty seconds and is the best answer to "how do I know this is
safe?".

1. Open your **Downloads** folder.
2. Hold **Shift**, right-click an empty area of the folder, and choose
   **Open PowerShell window here** (on Windows 11 it may say **Open in
   Terminal**).
3. Type this and press **Enter** (change the version number to match your
   file):

   ```
   Get-FileHash .\Hearth-Setup-0.1.0.exe
   ```

4. You will see a long line of letters and numbers under **Hash**.
5. Compare it with the one listed on the
   [release page](https://github.com/EricFinland/hearth-windows/releases/latest),
   in the notes and in the `SHA256SUMS.txt` file. PowerShell shows it in
   CAPITAL letters and the release page in small letters; that does not
   matter, they are the same hash. You do not need to check every character;
   the first eight and the last eight are plenty.

**If they match**, the file is genuine. **If they do not match**, delete the
file, do not run it, and download it again from the link above.

## Step 3: run the installer and get past SmartScreen

Double-click **`Hearth-Setup-<version>.exe`** in your Downloads folder.

A blue full-screen window appears:

> **Windows protected your PC**
>
> Microsoft Defender SmartScreen prevented an unrecognised app from starting.
> Running this app might put your PC at risk.

The only button you can see says **Don't run**. Do not click it. Instead:

1. Click the small underlined **More info** link under the message.
2. The window now shows *App: Hearth-Setup-0.1.0.exe* and
   *Publisher: Unknown publisher*. That is expected: "unknown publisher" just
   means "no paid certificate".
3. A new button appears at the bottom: click **Run anyway**.

### If there is no "Run anyway" button

Some computers, usually work or school machines, are set up by their
administrator to block all unsigned programs. On those, there is no button to
click, and you will need to ask your IT department. Do not try to get around
a block your organisation put there.

On a personal computer, you can also try this:

1. Right-click the installer in your Downloads folder and choose
   **Properties**.
2. At the bottom of the **General** tab, if there is a checkbox labelled
   **Unblock**, tick it.
3. Click **OK**, then double-click the installer again.

### If your antivirus complains

Some antivirus products are suspicious of any new program that is not signed.
If yours quarantines the installer, check the file using Step 2 first. If the
hash matches, you can restore it from your antivirus's quarantine list and
add an exception, or
[open an issue](https://github.com/EricFinland/hearth-windows/issues) and
say which antivirus it was, so it can be reported as a false positive.

## Step 4: install

The installer is a normal setup wizard. Click **Next** / **Install** and then
**Finish**.

- It installs only for **your** Windows user account, into your own profile
  folder (`%LOCALAPPDATA%\Programs\Hearth`), so it does **not** ask for
  administrator rights.
- It adds Hearth to your **Start menu**.
- If your computer is missing Microsoft's WebView2 component (very rare on
  Windows 10 and 11), the installer fetches it for you.

## Step 5: first launch

Open **Hearth** from the Start menu.

The first launch takes a little longer than later ones. Hearth looks at your
computer's memory and graphics card, and if you have a supported graphics
card it downloads the matching version of its engine so models run faster.
You will see what it is doing; you do not need to do anything.

## Step 6: get a model

Hearth does not come with a model, because the right one depends on your
computer. Open the **model shop** inside the app.

Every model is marked to show whether it fits **your** machine. Pick one
that fits and click download. The progress bar is real: it does not jump
backwards, and you can cancel it.

**Not sure which one?** Choose a **7B model at Q4**. It fits comfortably in
8 GB of memory and is good enough to see whether Hearth is useful to you.

## Step 7: try it

Choose a folder for Hearth to work in. Hearth can only read and change files
**inside the folder you choose**, nowhere else on your computer.

Start with something you can check by eye, for example:

> Read every file in this folder and write a README.md that lists what each
> one does, in one sentence each.

By default Hearth **asks you before it changes any file or runs any command**,
and shows you what it wants to do first. Read what it asks for. That is the
most useful habit to build.

Before giving Hearth anything important, read
[the limitations page](limitations.md). Local models are less capable than
big online AI services, and that page is honest about where the edges are.

## Updating

**From the version after 0.1.1 on, Hearth updates itself.** Each time it
starts, it checks the
[releases page](https://github.com/EricFinland/hearth-windows/releases) for a
newer version. When there is one, a banner at the top of the window says
**Hearth X.Y.Z is available**:

1. Click **Install now**. Hearth downloads the update and checks that it
   really came from this project (it is signed with a key built into Hearth).
2. A small window shows the version and asks once more. Click
   **Install and restart**.
3. Hearth closes, updates, and opens again by itself. Your models, chats and
   settings are kept.

Click **Later** to be reminded next time instead. If your computer is offline,
nothing pops up; Hearth simply tries again next time. The **Updates** panel at
the bottom of the left sidebar always shows where things stand, has a
**Check for updates** button, and has a checkbox to turn the automatic check
off.

**If you have Hearth 0.1.1 (or older), update by hand once.** Those versions
cannot update themselves. Download the newest installer from the
[releases page](https://github.com/EricFinland/hearth-windows/releases) and run
it the same way you installed Hearth the first time. Your models and settings
are kept. After that, every update happens inside the app.

To hear about new versions by email as well, click **Watch** at the top of
[the repository](https://github.com/EricFinland/hearth-windows), then
**Custom**, then tick **Releases**.

## Uninstalling

Open **Settings**, then **Apps**, then **Installed apps** (on Windows 10:
**Apps & features**), find **Hearth**, and click **Uninstall**.

Downloaded models are stored under `%LOCALAPPDATA%\Hearth`. If you want that
disk space back after uninstalling, paste `%LOCALAPPDATA%` into the File
Explorer address bar and delete the **Hearth** folder.

## Why is it safe to click through the warning here?

A fair question, because "click past the security warning" is normally bad
advice. The difference is that with Hearth you do not have to take anyone's
word for it:

- **The code is public.** Anyone can read every line of it in this
  repository.
- **The installer is built in public.** GitHub's own servers build it from
  that code using a [public workflow](../.github/workflows/build.yml), and
  the log of every build is visible to anyone.
- **The file can be checked.** Step 2 proves the file you have is the one
  GitHub built.
- **GitHub vouches for where it came from.** Each installer carries a GitHub
  build attestation. People comfortable with the command line can confirm it
  was produced by this repository at a specific commit with
  `gh attestation verify Hearth-Setup-0.1.0.exe -R EricFinland/hearth-windows`.
- **You can build it yourself.** [The build guide](getting-started.md) does
  exactly what GitHub does, on your own machine.

What none of that protects you from is a **fake copy** from somewhere else.
Only ever download Hearth from
[github.com/EricFinland/hearth-windows/releases](https://github.com/EricFinland/hearth-windows/releases).
A "Hearth" installer from any other website, file-sharing service or email
attachment is not a Hearth release.

[The code signing policy](code-signing-policy.md) explains what a certificate
would add and why there is not one yet.

## Something went wrong?

[Open an issue](https://github.com/EricFinland/hearth-windows/issues) and
describe what happened and what you saw on screen. A screenshot helps. Hearth
keeps its logs in `%LOCALAPPDATA%\Hearth`; if you attach anything from there,
look through it first, because file paths can include the names of files you
opened.
