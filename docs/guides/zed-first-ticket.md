# From nothing to your first finished ticket, in Zed

This is the whole product in about ten minutes of your time: install an
extension, create one ticket, and watch a fleet claim it, do the work, submit
evidence, review it independently, and hand it back to you ready to merge —
without leaving Zed's Agent Panel.

Follow it in order the first time. Every step below was run on a real machine
before it was written down, and the failure notes are failures that actually
happened, not ones we imagined.

For the reference material — every command, every setting, the ACP thread, the
full troubleshooting table — see [Use Pursers from Zed](zed.md). This page is
the narrative; that page is the manual.

## What you need before you start

Zed 1.20.2 or later, and a network connection.

You also need [uv](https://docs.astral.sh/uv/) on your `PATH` and a complete
Pursers fleet, not just a running Central. The board must already exist, with
at least one onboarded, online worker advertising `can_work=true` and one
online reviewer advertising `can_review=true`. The reviewer must authenticate
as a different principal from the worker, or strict review cannot approve the
ticket.

If you are starting from nothing, follow [Run a multi-agent
fleet](running-a-fleet.md) first. It creates the board, credentials, memberships,
and seat identities, and starts the worker and reviewer push loops. Return here
only after those loops are running. A bare Central created with these two
commands is useful for connection testing, but it cannot finish this walkthrough
by itself:

```sh
uvx --from pursers-central pursers-central init ./pursers-local
uvx --from pursers-central pursers-central run ./pursers-local
```

`init` writes `./pursers-local/worker.jwt`. That file is a credential. You will
give Zed the **path** to it and never the contents.

Leave `run` going in its own terminal. Both uv and first-chat Central setup are
prerequisites we are removing; until that work lands, set them up before opening
Zed.

## 1. Install the extension

When Pursers is listed in Zed Extensions, search for **Pursers** and select
**Install**.

If you are working from a checkout instead, open the Command Palette, run
**zed: install dev extension**, and select `integrations/zed/pursers-mcp`.

Zed compiles the extension and lists it as **Pursers** with an **MCP Servers**
badge.

Copy any existing Pursers entry from `settings.json` before uninstalling the
extension. Uninstalling empties `context_servers` without warning, and the UI
has no undo.

## 2. Point it at your board

On the Zed Extensions page, find **Pursers** and select **Configure**.

Replace the form with your own values:

```json
{
  "central_url": "http://127.0.0.1:8766/mcp",
  "board_id": "pursers-local",
  "token_file": "/absolute/path/to/pursers-local/worker.jwt",
  "uvx_path": "/opt/homebrew/bin/uvx"
}
```

`token_file` must be a real absolute path. The form arrives holding
`/PATH/TO/PURSERS/worker.jwt`, which is an example, not a path — leaving it
there is the single most common way a first install fails.

If the relay does not start, set `uvx_path` to the full path printed by
`which uvx`. When Zed cannot launch a bare `uvx` name, it says nothing useful:
the server never answers and eventually times out.

Two things about this dialog are worth knowing before you use it again:

- Saving writes the box over your settings **verbatim**. Any key you remove from
  the box is deleted from your configuration.
- Every fresh open shows the shipped `pursers-local` example; it does not load
  the values saved in `settings.json`. Values you type are visible only in that
  open dialog. Cancel and reopen it, and the shipped example is back.

Once you have a configuration that works, prefer editing `settings.json`
directly and treat this dialog as a first-run convenience. If you reopen it,
cancel without saving; otherwise the example replaces the working setup,
including the token-file placeholder.

## 3. Confirm the connection

Back in **Settings → AI → MCP Servers**, Pursers should show a green state dot.

Then open the Agent Panel, start a thread, and type `/`. Five Pursers commands
appear:

| Command | What it does |
| --- | --- |
| `/board` | Board summary and everything waiting on you |
| `/create` | Create one ticket from a plain summary |
| `/watch` | Watch the board until something matters |
| `/evidence` | The exact commit, files, tests and review state for one ticket |
| `/answer` | Answer a question a seat asked you |

Run `/board`. On a brand-new board you get the board ID and a row of zeroes.
That is the correct answer, and it means the whole path works: Zed reached the
relay, the relay reached Central, and Central checked your credential.

It does **not** prove that a worker or reviewer is online. Before `/create`, ask
the fleet operator to verify that the board exists, a `can_work=true` worker is
onboarded and running its push loop, and a `can_review=true` reviewer on a
different principal is onboarded and running its review loop. If any one is
missing, stop here and
finish [the fleet setup](running-a-fleet.md#4-run-each-seat-as-a-push-loop);
the ticket can be created, but it cannot travel all the way to `ready to merge`.

If a first attempt fails with an authorization error rather than a timeout,
that is also good news — it proves the round trip. You are a member problem
away, not a connection problem away.

## 4. Create the first ticket

```text
/create Add a --version flag to the CLI that prints the package version and exits 0
```

Write the summary the way you would brief a colleague: what should be true when
it is done. Do not write the implementation.

Pursers asks for anything required that it cannot infer, then shows you the
ticket it is about to create. Zed raises its own permission prompt before the
write — read it, then allow it. Every board write goes through that prompt;
nothing is written on your behalf without it.

You get back a ticket ID like `TK-4a7c19e0b3d2`. That ID is how you refer to
this work everywhere from now on.

## 5. Watch it get picked up

```text
/watch
```

Keep the turn running. When an eligible `can_work=true` worker's push loop is
online, the board normally offers your ticket within seconds and that seat
claims it. `/watch` reports the claim with the seat's name. If no eligible
worker is online, the ticket stays open until one connects.

What happens next happens without you. The seat reads the ticket, works in its
own clone, and commits. You are not in the loop and do not need to be.

`/watch` ends its turn the first time something genuinely needs you — a
question, a verdict, a failure — so that Zed's normal completion notification
fires. That is deliberate: Zed has no way to interrupt you from a background
MCP server, so ending the turn *is* the notification. Run `/watch` again for
the next one.

## 6. A seat asks you something

Sooner or later a seat hits a decision that is yours, not its. It asks, and
stops. It does not guess.

`/watch` surfaces the question with the seat name, the ticket, the question
itself, how long it has been waiting, and a short reference like `#1`.

```text
/answer
```

A form opens holding the question and the fields the ticket requires. Fill it
in, accept, and approve the write when Zed asks. If several questions are
pending, name one — `/answer #2`. Pursers never picks for you.

Declining the form, rejecting the permission, or closing the thread all leave
the question open on the board. Nothing is lost and nothing is assumed.

## 7. Read the evidence before you trust it

When the seat submits, look at what it actually did:

```text
/evidence TK-4a7c19e0b3d2
```

You get the branch, the commit SHA, every changed file, the literal test output,
and the review state. Not a summary of the work — the work.

## 8. The verdict, from someone else

With an eligible review loop online, your ticket is now reviewed by a
**different authenticated principal** from the one that built it. That reviewer
can reject it, and frequently does. A rejection comes back with the specific
reason attached, an eligible worker picks the ticket up again, fixes it, and
resubmits. You will see that whole exchange in `/board` and `/evidence`.

Only after an independent approval does the ticket reach `ready to merge`, with
you named as the person it is waiting on.

Merge it through your normal repository workflow. The extension deliberately
adds no merge button: the last step stays yours.

## What you just watched

One sentence of intent became a tracked ticket, a claimed lease, real commits,
literal test output, an independent review with the authority to say no, and a
finished change waiting for your approval.

You typed five commands and answered one question. The rest ran on its own, and
every step left evidence you can read.

## When it goes wrong

| What you see | What it means |
| --- | --- |
| The Configure dialog spins on **Connecting Server**, then Zed logs `ERROR [project::context_server_store] pursers context server failed to start: Context server request timeout` | The relay did not start. Set `uvx_path` to the absolute path printed by `which uvx`, then restart the server. |
| The next line is `ERROR [crates/context_server/src/transport/stdio_transport.rs:58] Broken pipe (os error 32)` | The same failed launch. Zed never says that it could not start `uvx`; this transport error is the second visible symptom. |
| A fresh Configure dialog shows `pursers-local` instead of your working values | That dialog always starts from the shipped example. Cancel without saving and edit the Pursers object in `settings.json`; saving would replace the whole entry verbatim. |
| Zed's log says `Pursers settings are invalid: missing field central_url` | Zed passed the extension nothing. Usually your `context_servers` entry is empty or was wiped — check `settings.json` before you check anything else. |
| Pursers has no state dot and no log output at all | The project is in Restricted Mode, which blocks every MCP server. Click **Trust and Continue** in the banner. |
| Your configuration vanished | Uninstalling the extension empties `context_servers`. Copy the entry out of `settings.json` before you uninstall anything. |
| A command is missing from `/` | Check for a green state dot. If another MCP server uses the same prompt name, Zed prefixes ours: `/pursers.board`. |

The full table, including TLS and token problems, is in [Use Pursers from
Zed](zed.md#troubleshooting).

## Recording this as a walkthrough

The steps above are ordered so that screen-recording them straight through
produces a usable demo. Suggested shots:

1. Extensions page, install, the Pursers row appearing.
2. The Configure dialog, filled in, saved, green dot.
3. `/` in the Agent Panel showing five commands.
4. `/board` on an empty board — zeroes.
5. `/create`, the permission prompt, the ticket ID.
6. `/watch` catching the claim, with the seat's name visible.
7. The question arriving, `/answer`, the form.
8. `/evidence` — commit, files, test output on screen.
9. The rejection, the fix, the approval.
10. `ready to merge · you`.

Shot 9 is the one worth the most screen time. A review that can say no, from a
seat that did not write the code, is the part nobody else is doing.
