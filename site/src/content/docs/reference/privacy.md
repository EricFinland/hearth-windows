---
title: Privacy
description: Every destination Hearth contacts, and why.
---

Hearth is a desktop application that runs language models on your own machine.
This page states exactly what data it handles, what leaves your computer, and
what the project collects. It is written to be checkable against the source
rather than believed.

Short version: **Hearth has no telemetry, no analytics, no crash reporting and
no accounts. The project collects nothing about you.** Everything below is
detail on that claim and on the connections the application does make, all of
which are either started by you or needed to fetch software you asked for.

## What the project collects

Nothing.

There is no analytics SDK, no crash reporter, no usage ping, no update
heartbeat that carries an identifier, and no account system. There is no server
operated by this project that receives anything from an install. You can verify
the negative the same way anyone else can: every network call in the shipped
code goes through the modules listed below, and there is no other outbound path.

## What stays on your machine

All of it, unless a section further down says otherwise.

- **Your prompts and the model's replies.** Inference runs against a model file
  on your own disk, executed by a llama.cpp server that ships inside the
  installer and binds to loopback.
- **Your files.** The agent's file tools resolve every path through
  `agent/hearth_contain.py` and operate on your local disk.
- **The audit record.** Runs, tokens, latency, errors and tool calls are
  written to a local SQLite database in Hearth's own data directory (see
  `agent/hearth_paths.py`). It is never uploaded.
- **Checkpoints.** `agent/hearth_checkpoint.py` keeps a shadow git store on
  your disk so a turn can be undone. It never has a remote.
- **Downloaded models.** Stored locally and reused.

## What leaves your machine, and only when it has to

Every item here is triggered by something you did, and none of it carries an
identifier the project assigned to you.

| Destination | When | What is sent |
| --- | --- | --- |
| `huggingface.co` | you browse or download a model in the model shop | ordinary HTTPS requests for repository metadata and model files. No account is required and none is used unless you supply a token yourself |
| `github.com` | first launch, and when you change GPU engine | a download of the pinned llama.cpp release named in `vendor/llama_manifest.json`, verified by checksum |
| the update feed in `release/trust.json` | an update check | a request for a signed release manifest. **This is currently pointed at a reserved name that can never resolve**, so no install can reach an update host at all. See [updates.md](/hearth-windows/concepts/updates/) |
| `html.duckduckgo.com` | you (or the agent, with your approval) use the web search tool | the search query |
| a URL you give it | the agent fetches a page you or it named, with your approval under the active permission mode | an ordinary HTTPS request |
| `ntfy.sh`, or a Telegram bot | only if you configure notifications yourself | the notification text you configured |

Nothing in that table happens on a schedule you did not set, and nothing in it
sends your prompts, your files, or your audit database.

## Third-party services and their own terms

When Hearth fetches from Hugging Face, GitHub or DuckDuckGo, those are ordinary
requests to services this project does not operate. They see what any HTTP
service sees: your IP address, the request, and your network's routing. Their
privacy terms apply to that, not this page. Hearth adds no identifier of its
own to those requests.

## Model providers

Hearth runs local models. If a future version lets you configure a hosted model
provider, prompts you send through it go to that provider under their terms,
and this page will be updated to say so before that ships. No such path exists
today.

## Children

Hearth is a developer tool and is not directed at children.

## Changes

This statement is a file in the repository. Its history is the change log, and
any revision is visible in the same commit history as the code it describes.

## Contact

Questions about this statement, or a claim that it is inaccurate, belong in a
GitHub issue on this repository. If the inaccuracy is a security problem,
follow [SECURITY.md](/hearth-windows/project/security/) instead.
