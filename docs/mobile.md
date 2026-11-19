# Using microtask from your phone

Your cards live in a private Git repository, so anything that can reach that
repository can work with them. There are four ways to use microtask from your
phone, from least to most setup:

1. **[Claude or Codex cloud apps](#claude-or-codex-cloud-mobile-apps-no-server)**:
   an agent in the cloud works on your private repository.
2. **[ChatGPT with the GitHub connector](#chatgpt-github-connector--mt-import-github)**:
   capture issues from ChatGPT and import them on your computer.
3. **[`mt web` over Tailscale](#mt-web-over-tailscale)**: the dashboard from
   your own machine, in your phone's browser.
4. **[A Telegram bot with Claude Code](#telegram-bot-with-claude-code-on-a-raspberry-pi-or-vps)**:
   chat and voice notes with an agent on a Raspberry Pi or small server.

---

## Claude or Codex Cloud mobile apps (no server)

Connect your **private GitHub repository** to one of these apps:

* **ChatGPT mobile app (Codex Cloud)** via the GitHub connector
* **Claude mobile app** (Code tab / `claude.ai/code`) via the Claude GitHub App

When you send a prompt from your phone, the service starts a container, clones
your repository, and reads its `AGENTS.md`. Your private repository holds only
your cards, so add an `AGENTS.md` to it that tells the agent to install
microtask first:

````markdown
# Tasks

This repository holds microtask cards. Before working with them, install
microtask for this session:

```sh
curl -fsSL https://raw.githubusercontent.com/pablormier/microtask/main/install.sh | bash -s -- --yes --no-path --dir /tmp/microtask --workspace "$PWD"
```

Then read `/tmp/microtask/AGENTS.md` and follow it. Run every command from
this repository's root as `/tmp/microtask/mt -R . COMMAND`.
````

Commit it once, from your computer:

```sh
mt run git add AGENTS.md
mt run git commit -m "Add instructions for cloud agents"
mt sync
```

**Trade-offs compared to a self-hosted bot:**

* **No server to maintain**, works when your PC is off.
* **30–90 second startup latency** per task.
* **Pull-request workflow:** Cloud sandboxes push to a branch rather than
  `main`, so you'll tap **Merge** in the app or GitHub Mobile before running
  `mt sync` on your computer.
* **No local `.mt` state:** Machine-local state such as the pinned `mt focus`
  is not shared. Running `mt time` timers are part of their cards, so they sync.

---

## ChatGPT GitHub connector + `mt import-github`

For quick capture from the ChatGPT app, without a cloud container or pull
requests, let a ChatGPT Project file GitHub issues, then import them on your
computer:

1. **Create a ChatGPT Project:** Enable the GitHub connector, link it to your
   private repository, and create a `microtask-capture` label there. Add these
   **Project instructions**, replacing `<owner>/<repo>` with that repository:

   ```text
   Use the GitHub connector to create issues in `<owner>/<repo>` (unless the user specifies another repo).
   Before creating an issue, read https://github.com/pablormier/microtask/blob/main/.github/ISSUE_CAPTURE.md and follow its rules. Label every capture issue `microtask-capture`. You only create the GitHub issue; it is imported later.
   For a paper link, follow https://github.com/pablormier/microtask/blob/main/.agents/skills/mt-paper-review/SKILL.md and put the review in the issue body.
   Always write the issue title and body in English, and ask the user to confirm the proposed title, body, and label before creating the issue.
   ```

2. **On your phone:** Chat inside that Project naturally (for example, *"Capture:
   renew passport before March"* or paste a link to a paper) and confirm the
   proposed issue.

3. **On your computer:** Run `mt import-github` (requires the
   [GitHub CLI](https://cli.github.com/) `gh` authenticated for your repo):

   ```sh
   mt import-github
   ```

   This saves each open `microtask-capture` issue as a note marked
   `needs_review`, then closes the issue. Triage the notes with
   `mt list --needs-review` or the `mt-triage` skill. See [.github/ISSUE_CAPTURE.md](../.github/ISSUE_CAPTURE.md)
   for the full capture and triage rules.

---

## `mt web` over Tailscale

`mt web` serves a responsive dashboard that works on mobile browsers. However,
`mt web` uses an ephemeral per-session token embedded in the HTML and has **no
user login**, so it must **not** be exposed directly to the public internet.

To access it securely from your phone:

1. Run `mt web` on an always-on machine connected to your private
   [Tailscale](https://tailscale.com/) network, binding to its Tailscale IP:

   ```sh
   mt web --host <YOUR_TAILSCALE_IP> --port 8080 --no-open
   ```

2. Open `http://<YOUR_TAILSCALE_IP>:8080` in your phone's browser and use
   **Add to Home Screen** for quick access. With `rules.auto_sync: true`,
   `mt web` syncs your edits with `origin` in the background.

`mt web` answers only requests whose `Host` header is `localhost`, `127.0.0.1`,
`[::1]` or the `--host` address, each with the server's port. Anything else gets
`421`. This stops a web page from reaching the dashboard through a DNS name that
points at your machine. With `--host 0.0.0.0` or `--host ::`, any IP address is
accepted, but DNS names are not, so open the dashboard by IP address.

Behind a reverse proxy that forwards the public `Host` (Caddy does by default),
pass `--allow-host tasks.example.com` (repeatable; `NAME:PORT` also pins the
port, while a bare name accepts any port). Otherwise configure the proxy to send
the backend address as `Host`.

---

## Telegram bot with Claude Code on a Raspberry Pi or VPS

Claude Code's Telegram plugin, running on an always-on Linux machine such as a
Raspberry Pi or a small VPS, gives you chat and voice notes with an agent that
answers right away and can run `mt sync` itself. Claude reads
[AGENTS.md](../AGENTS.md) and the skills in the app folder and runs `./mt`.

### 1. Server prerequisites

SSH into your Raspberry Pi or VPS and check that it is **64-bit** (`aarch64` or
`x86_64`):

```sh
uname -m
```

*(On a Raspberry Pi, this must print `aarch64`. If it prints `armv7l`, you
need 64-bit Raspberry Pi OS.)*

Ensure `git`, `python3`, and `tmux` are installed, then install **Claude Code**
and **Bun** (Bun is required by the official Telegram channel plugin):

```sh
curl -fsSL https://claude.ai/install.sh | bash
curl -fsSL https://bun.sh/install | bash
source ~/.bashrc
```

Install microtask and give the installer your **private** repository when it
asks; it clones your cards into `~/microtask/workspace`. Then verify `./mt`
works and can push/pull without interactive password prompts (for example,
using an SSH key):

```sh
curl -fsSL https://raw.githubusercontent.com/pablormier/microtask/main/install.sh | bash
cd ~/microtask
./mt info
./mt sync
```

### 2. Log in to Claude on a headless server

Because a Raspberry Pi or VPS usually has no browser for OAuth login, generate
a token on your laptop/desktop (where you have a browser) and copy it to the
server:

1. **On your laptop/desktop**, run:

   ```sh
   claude setup-token
   ```

   Complete the browser login flow; the command prints an OAuth token.

2. **On your Raspberry Pi / VPS**, save the token in your shell profile and mark
   onboarding complete:

   ```sh
   echo 'export CLAUDE_CODE_OAUTH_TOKEN="YOUR_TOKEN_HERE"' >> ~/.bashrc
   source ~/.bashrc
   echo '{"hasCompletedOnboarding": true}' > ~/.claude.json
   ```

   Test that Claude starts cleanly inside `~/microtask`:

   ```sh
   cd ~/microtask
   claude
   ```

   Once you see the prompt, type `exit`.

### 3. Create a Telegram bot

1. Open Telegram and message **`@BotFather`**.
2. Send `/newbot` and follow the prompts to choose a display name and a
   username ending in `bot` (for example, `my_microtask_bot`).
3. Copy the **API token** BotFather gives you.

### 4. Install and configure the Telegram plugin

Start Claude Code inside your `microtask` workspace:

```sh
cd ~/microtask
claude
```

Inside the Claude prompt, run:

```text
/plugin marketplace add anthropics/claude-plugins-official
/plugin install telegram@claude-plugins-official
/reload-plugins
/telegram:configure YOUR_BOT_TOKEN
```

Exit Claude (`exit`) once configuration completes.

### 5. Launch persistently and pair your phone

Start a `tmux` session so Claude keeps running after you close SSH:

```sh
tmux new -s microtask-bot
cd ~/microtask
claude --channels plugin:telegram@claude-plugins-official
```

Now pair your Telegram account:

1. Open Telegram, find your new bot, and send it `"Hi"`.
2. The bot replies with a **6-character pairing code**.
3. Back in the `tmux` terminal on your server, approve the code and lock access
   down to an allowlist so no one else can message your bot:

   ```text
   /telegram:access pair <6-CHAR-CODE>
   /telegram:access policy allowlist
   ```

4. Detach from `tmux` without stopping the bot by pressing `Ctrl+B`, then `D`.
   (To re-attach later: `tmux attach -t microtask-bot`.)

### 6. Smooth remote operation tips

* **Avoid terminal permission stalls:** When you message Claude from Telegram,
  it cannot click `[Allow]` in your server terminal if it asks permission to
  run `./mt`. To avoid stalling, pre-allow safe commands in
  `~/microtask/.claude/settings.local.json` (which is local to the server and not
  committed to Git):

  ```json
  {
    "permissions": {
      "allow": [
        "Bash(./mt *)",
        "Bash(git status*)",
        "Bash(git diff*)"
      ]
    }
  }
  ```

  *(Ask Claude in your Telegram chat to run `./mt sync` when you want changes
  pushed, or tell it your preference so it remembers to sync after edits.)*

* **Auto-start on reboot (`systemd`):** Once you've tested the setup in `tmux`,
  you can run the `tmux` command from a `systemd --user` service (with
  `WorkingDirectory=%h/microtask` and `Restart=always`) so it comes back
  automatically if your Pi reboots.

### 7. Example prompts from Telegram

* *"Add a high-priority task to renew my passport before March."*
* *"Triage my inbox."*
* *"What should I focus on for the next 30 minutes?"*
* *"Mark the dentist task done and run mt sync."*
