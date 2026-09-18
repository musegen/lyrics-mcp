# lyrics-mcp

An MCP server that writes original song lyrics with Claude.

Give it a brief, get back a title and a full set of lyrics marked up with English
section tags — `[Verse]`, `[Pre-Chorus]`, `[Chorus]`, `[Bridge]`, `[Outro]` — that stay
English whatever language the lyrics themselves are in. That is the format music
generation models expect, so the output pastes straight in.

Bring your own Anthropic API key. There is no hosted service behind this: the server
runs on your machine, calls the Anthropic API with your credentials, and sends nothing
anywhere else.

## Quick start

There is nothing to install. Add this to your MCP client's config — for Claude Code,
`.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "lyrics": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/musegen/lyrics-mcp.git", "lyrics-mcp"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

Restart your client and ask it to write you a song.

This needs [uv](https://docs.astral.sh/uv/), which builds the server in a throwaway
environment on first launch and handles Python for you. If you would rather not put
your key in the config file, drop the `env` block and export `ANTHROPIC_API_KEY` in
the environment your client launches from.

## Other ways to run it

**Installed on your PATH.** Python 3.10 or newer:

```bash
pip install git+https://github.com/musegen/lyrics-mcp.git
```

```json
{
  "mcpServers": {
    "lyrics": {
      "command": "lyrics-mcp",
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    }
  }
}
```

Note that `command` is resolved against the PATH your MCP client sees, which is not
always the shell you installed from — a virtualenv or a `--user` install often lands
somewhere the client cannot find. If the server fails to start, use the absolute path
to the `lyrics-mcp` executable, or go back to `uvx`.

**From a clone**, for hacking on it:

```json
{
  "mcpServers": {
    "lyrics": {
      "command": "python",
      "args": ["-m", "lyrics_mcp.server"],
      "cwd": "/path/to/lyrics-mcp",
      "env": { "PYTHONPATH": "src", "ANTHROPIC_API_KEY": "sk-ant-..." }
    }
  }
}
```

## The tool

`write_lyrics` — one required argument, four optional ones.

| Argument | Required | Description |
| --- | --- | --- |
| `brief` | yes | What the song is about. Concrete imagery beats abstraction: *"walking home alone through a quiet city after a late shift"* gets a much better song than *"a sad song"*. |
| `language` | no | Language for the lyrics (`English`, `Chinese`, `Japanese`, ...). Defaults to the language of the brief. |
| `genre` | no | `indie folk`, `synth pop`, `trap`, ... |
| `mood` | no | `wistful but hopeful`, `defiant`, ... |
| `structure` | no | `ABABCB`, or a spelled-out section order. Left empty, the model picks one that fits the emotion. |

Returns `{"title": ..., "lyrics": ...}`.

## Configuration

Everything is an environment variable. Only the first one is required.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Your API key. The Anthropic SDK also accepts `ANTHROPIC_AUTH_TOKEN` and an `ant auth login` profile. |
| `ANTHROPIC_BASE_URL` | Anthropic API | Point the SDK at a gateway or proxy. |
| `LYRICS_MCP_MODEL` | `claude-opus-5` | Which model writes the lyrics. |
| `LYRICS_MCP_FALLBACK_MODEL` | `claude-opus-4-8` | Where a declined request is retried (see below). Set to empty to disable. |
| `LYRICS_MCP_RPM` | `10` | Requests per minute this process will make. |

### On the rate limit

`LYRICS_MCP_RPM` is a token bucket over this process's API calls. It exists because an
agent that gets stuck in a loop on this tool can spend a lot of your money quickly, and
ten calls a minute is more than any human songwriting session needs.

It is not a security control. It lives in the process, so it caps your own runaway
loops and nothing else — every user runs their own copy and spends their own money.

### On refusals

Safety classifiers sometimes decline a request, which is a real possibility for lyrics
about grief, violence or addiction. By default the server enables server-side fallbacks,
so a declined request is re-run on `LYRICS_MCP_FALLBACK_MODEL` inside the same API call
and you usually never notice. If the whole chain declines, you get a clear error, and
softening the wording of the brief normally gets a result.

Gateways that have not adopted the fallback beta will reject it; the server notices,
logs a line to stderr, and carries on without it.

### On cost

Every call is billed to your key at your model's rates. A song is typically a few
thousand output tokens. Check [Anthropic's pricing](https://www.anthropic.com/pricing)
for the current numbers, and set `LYRICS_MCP_MODEL` to a smaller model if you would
rather trade some quality for cost.

## Development

`smoke_test.py` checks prompt assembly, output parsing, the rate limiter and the tool
schema. It needs no API key and makes no network calls:

```bash
pip install -e .
python smoke_test.py
```

## License

MIT — see [LICENSE](LICENSE).
