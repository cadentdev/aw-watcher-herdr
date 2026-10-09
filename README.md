# aw-watcher-herdr

An [ActivityWatch](https://activitywatch.net) watcher for [herdr](https://github.com/herdrdev/herdr), the terminal multiplexer for AI coding agents.

It records two things:

- **Your focus**: which herdr workspace, tab and pane you're in, its working directory and project, and which agent is running there.
- **Your agents' activity**: when each agent is `working` or `blocked`, even while you're looking at something else. You can see how long Claude spent on a project separately from how long you did.

It has no dependencies beyond Python 3.9+ and the `herdr` CLI.

## Install

```sh
pipx install git+https://github.com/cadentdev/aw-watcher-herdr
```

Check it can see herdr, without sending anything to ActivityWatch:

```sh
aw-watcher-herdr --dry-run --once
```

Then run it:

```sh
aw-watcher-herdr
```

### Run at login (macOS)

Copy [`launchd/aw-watcher-herdr.plist`](launchd/aw-watcher-herdr.plist) to `~/Library/LaunchAgents/`, replace `YOUR_USERNAME` with your username, then:

```sh
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/aw-watcher-herdr.plist
```

The watcher waits for the ActivityWatch server if it isn't up yet, and records nothing while herdr isn't running.

## Buckets and events

The bucket names use the hostname reported by your ActivityWatch server.

### `aw-watcher-herdr_<hostname>` (type `herdr.focus`)

Built from heartbeats every `--poll` seconds (default 5). Consecutive identical polls merge into one event.

```json
{
  "workspace": "work",
  "tab": "agents",
  "cwd": "/home/me/src/widget/docs",
  "project": "widget",
  "agent": "claude",
  "title": "Write the docs"
}
```

`project` is the name of the git repository containing `cwd`. If `cwd` isn't in a repository, it's the directory name. `agent` and `title` only appear when the focused pane is running an agent.

### `aw-watcher-herdr-agents_<hostname>` (type `herdr.agents`)

One event for each continuous period an agent spends in the `working` or `blocked` state. Agents that are `idle`, `done` or `unknown` aren't recorded. Events from different agents can overlap. Long periods are split every `--max-span` seconds (default 300) so they show up promptly.

```json
{
  "workspace": "work",
  "tab": "agents",
  "cwd": "/home/me/src/gadget",
  "project": "gadget",
  "agent": "codex",
  "status": "working",
  "title": "Fix flaky test"
}
```

Use `--no-agents` to turn this bucket off.

## Example queries

Paste these into ActivityWatch's **Query** view.

Time you spent in herdr per project, counting only time when the terminal was the active window and you weren't AFK. Change `"iTerm2"` to your terminal app's name as ActivityWatch sees it (e.g. `"Ghostty"`, `"Terminal"`, `"WezTerm"`):

```
afk = flood(query_bucket(find_bucket("aw-watcher-afk_")));
not_afk = filter_keyvals(afk, "status", ["not-afk"]);
window = filter_period_intersect(flood(query_bucket(find_bucket("aw-watcher-window_"))), not_afk);
terminal = filter_keyvals(window, "app", ["iTerm2"]);
herdr = filter_period_intersect(flood(query_bucket(find_bucket("aw-watcher-herdr_"))), terminal);
RETURN = sort_by_duration(merge_events_by_keys(herdr, ["project"]));
```

Agent working time per agent and project:

```
agents = query_bucket(find_bucket("aw-watcher-herdr-agents_"));
working = filter_keyvals(agents, "status", ["working"]);
RETURN = sort_by_duration(merge_events_by_keys(working, ["agent", "project"]));
```

## Limitations

- herdr only knows which pane is focused *inside herdr*, not whether your terminal window is in front. Combine the bucket with the window and AFK buckets, as in the first query above.
- herdr's focus reflects its last attached client, so time can keep counting while you're detached. The window filter above handles that as well.
- ActivityWatch has no built-in view for custom watchers. Use the Timeline, categories or the Query view.

## Options

```
--server URL       ActivityWatch server (default http://127.0.0.1:5600)
--testing          use the testing server on port 5666
--poll SECONDS     polling interval (default 5)
--max-span SECONDS split agent events after this long (default 300)
--no-agents        don't record agent activity
--herdr PATH       path to the herdr binary
--dry-run          print events instead of sending them
--once             poll once and exit
-v, --verbose      debug logging
```

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m aw_watcher_herdr --dry-run --once
```

## License

MIT. See [LICENSE](LICENSE).
