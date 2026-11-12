# Sourced by triage.tape from the repository root before recording. Builds a
# throwaway workspace in /tmp/mt-demo/todo holding only three raw captures, and
# starts Claude Code in auto mode so safe commands run without a permission
# prompt. Your real cards and Claude settings are not touched.
MT_DEMO_ROOT=/tmp/mt-demo/todo
. docs/demo/setup.sh || return 1
unset MT_DEMO_ROOT
# Start Claude like a fresh terminal would, even when recording from inside
# another Claude Code session.
unset $(env | grep -oE '^(CLAUDE[A-Z_]*|ANTHROPIC[A-Z_]*)=' | tr -d =)
rm -f cards/*.md
# Start empty instead of showing the bundled example cards.
sed -i.bak 's/demo_workspace: true/demo_workspace: false/' mt-policy.yaml && rm -f mt-policy.yaml.bak
command mt add "call dentist about the crown, sometime before end of month" >/dev/null 2>&1
command mt add "review Ana's Q3 budget slides before the Nov 20 meeting" >/dev/null 2>&1
command mt add "read the transformer paper before Friday's reading group https://arxiv.org/abs/1706.03762" >/dev/null 2>&1
mkdir -p .claude
cat > .claude/settings.local.json <<'JSON'
{
  "permissions": {
    "defaultMode": "auto"
  }
}
JSON
clear
