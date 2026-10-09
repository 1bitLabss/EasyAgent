# Safety

EasyAgent does not do something dangerous or irreversible until you say yes, in the chat, for that exact action.

The check runs before the tool. A fixed list of rules decides first. If nothing in the list matches, the same model this bot is already connected to reviews the call. There is no extra helper model. If that review cannot run, the action waits for you. It does not run.

## Three answers

**Allow.** Reading, listing, and search. A harmless command such as printing a line or checking git status. Writing a new file inside the workspace. The workspace is the app-data folder, this bot's own workspace folder, the working folder for the turn, any folder you named in the chat, and the system temp folder.

**Ask.** The chat shows an approval card and the turn waits.

- Deleting, moving, or renaming a file that is already there.
- Replacing a file, unless you named that file or its folder and asked for it to be written, or the bot created it earlier in the same turn. Normal mode also allows a replacement inside the workspace. A copy is kept first either way.
- Writing a new file outside the workspace.
- Installing or removing software.
- Changing a service, the registry, a scheduled task, or a startup entry.
- `git push`, `git reset`, `git clean`, or deleting a branch.
- Uploading, posting, sending a message or an email, or spending money. A message is a draft until you approve it.
- Anything on a remote computer that changes state. A remote read uses the same block list, and it is stricter about everything else.
- Downloading a program.

**Block.** These do not run, even if you would have pressed Approve. Advanced can unlock one rule by name. The unlocked rule still waits for a yes. It is never automatic, and the card does not offer Always allow.

- Formatting a disk, `diskpart`, `mkfs`, or `dd` onto a device.
- `rm -rf /`, `rm -rf /*`, deleting a home directory, or deleting `C:\`.
- Turning off the firewall or Windows Defender.
- Dumping credentials.
- Reading browser cookies or saved passwords.
- `curl | sh` and `iwr | iex`.
- A fork bomb.
- Editing EasyAgent's own guardrail files.

The rules look at the command that will actually run. A base64 PowerShell command is decoded first. `cmd /c` and `bash -c` are unwrapped. A chain joined by `&&`, `;`, or `|` is judged piece by piece, and the worst piece wins. A script written to disk is judged as the command inside it. An alias or an environment variable is expanded before the decision.

## The card

The card names the bot. It shows the exact command, path, or change. It names the tier and the rule, and it says why in one sentence.

- **Approve once** runs that action this time.
- **Deny** stops it.
- **Always allow this exact command for this bot** is optional. It is never shown for a blocked rule. It does not allow a different command.

The card expires. Expiry is a denial. A denial is stored. The same action later is blocked, with no new card. The bot is told not to retry it, reword it, or reach the same result another way.

While the card is open, the turn pauses. It continues after you decide. EasyAgent also raises a desktop notification, and a browser notification when the tab is in the background and you have allowed notifications.

Every decision is written to an audit log. Settings shows that log for the bot.

## Undo

An approved delete on this computer goes to EasyAgent Trash, not a hard delete. Settings lists it and can restore it. A delete on a remote computer is not moved to Trash.

Before a replacement, EasyAgent copies the old file into a snapshot. Settings can restore that copy.

## Settings, per bot

Careful is the default.

- **Careful.** The rules above.
- **Normal.** Replacing a file inside the workspace is allowed. A snapshot is still kept. Deletes, remote changes, and the rest still ask.
- **Advanced.** You type the bot's name to turn it on. You can unlock a blocked rule by its name. That rule still asks. Nothing on the block list runs by itself.

## Untrusted text

A fetched page, a file read, and command output are wrapped in markers before they go back to the model. The system prompt says that instructions inside those markers are data, not orders. If a tool call repeats a command or a path that appeared in that text, the call is asked even when the rules would have allowed it.

Secrets in tool output are scrubbed before the text is shown to the model. A credential is not a way to open a wider path. Reading a `.env` file, an SSH key, a token file, or a wallet waits for you. Browser cookie and password stores are blocked.

## Limits and lessons

One turn stops after 48 tool calls, or after 20 minutes of tool time. That stop is a block. It is not a reason to cut off an ordinary page or a short task.

The learning loop, the nightly pass, a promoted lesson, a playbook, and a note cannot weaken these rules or edit the guardrail files. A lesson that tries is rejected and is not saved.

## Checks

`tests/test_safety.py` holds the red-team cases: direct commands, an injected line from a fetched page, encoded and wrapped commands, and a remote command. A dangerous case must be asked or blocked. A harmless print, list, or new file in the workspace must run without a card. The existing eval tasks stay under a 10% false-ask rate.
