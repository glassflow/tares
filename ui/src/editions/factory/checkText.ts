// A milestone's check is free text: either a shell command the crew runs or a sentence written for a
// person. The record does not say which, so this reads the text. When in doubt it says sentence, so
// a person's words are not set in the command font.

const TOOLS = new Set([
  "awk", "bash", "bun", "cargo", "cd", "curl", "docker", "env", "eslint", "export", "gh", "git", "grep", "helm", "jq",
  "kubectl", "make", "mypy", "node", "npm", "npx", "pip", "pip3", "playwright", "pnpm", "psql", "pytest", "python",
  "python3", "rg", "ruff", "sed", "sh", "sqlite3", "sudo", "tares", "tsc", "uv", "uvx", "vite", "vitest", "wget", "yarn", "zsh",
]);

// Small words that show up in a lowercase sentence and almost never as a bare argument of a command.
const PROSE = new Set([
  "a", "an", "and", "are", "at", "be", "by", "can", "from", "is", "it", "its", "must", "not", "of", "on", "or", "should",
  "sure", "that", "the", "these", "this", "those", "to", "was", "were", "will", "with", "you", "your",
]);

export function looksLikeCommand(text: string): boolean {
  const s = text.trim().replace(/^\$\s+/, "");
  // a backtick marks a command inside a sentence; closing punctuation ends a sentence
  if (!s || s.includes("`") || /[A-Za-z)][.!?:]$/.test(s)) return false;
  if (/^[A-Za-z_]\w*=\S/.test(s)) return true;    // FOO=1 npm test, T=$(mktemp -d); ...
  const head = s.split(/\s+/, 1)[0];
  if (!/^[a-z0-9_.~/-]+$/.test(head)) return false;    // a sentence starts with a capital
  if (/&&|\|\||\s\|\s|\$[({A-Za-z_]/.test(s)) return true;
  const args = s.replace(/"[^"]*"|'[^']*'/g, " ").split(/\s+/).slice(1);    // quoted text may be prose
  if (args.some((a) => PROSE.has(a.toLowerCase()))) return false;
  return TOOLS.has(head)
    || /^(?:\.{1,2}|~)\/|^[\w.-]+\/[\w./-]+$/.test(head)    // ./run.sh, scripts/check.sh
    || args.some((a) => /^--?[A-Za-z]/.test(a));
}
