module.exports = {
  run: [
    {
      // Pull the latest launcher + app source (only if this folder is a
      // git repo — plain Pinokio api folders are not, and `git pull` would
      // abort the whole update otherwise).
      when: "{{exists('.git')}}",
      method: "shell.run",
      params: {
        message: "git pull"
      }
    },
    {
      // The vpipe engine is committed to this repo, so `git pull` (above)
      // already refreshed it — no download/extract step. Re-assert the exec
      // bit in case the pull touched the CLI.
      method: "shell.run",
      params: {
        message: "chmod +x app/vpipe/vpipe"
      }
    },
    {
      // Refresh Python dependencies in case requirements.txt changed.
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "uv pip install -r requirements.txt"
        ]
      }
    }
  ]
}
