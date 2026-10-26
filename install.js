module.exports = {
  requires: {
    bundle: "ai"
  },
  run: [
    {
      // The vpipe engine (vpipe/vpipe CLI + FFmpeg dylibs) is committed to
      // the repo, so there is no download/extract step. Git tracks the
      // exec bit, but this is a cheap safety net so the CLI is runnable.
      method: "shell.run",
      params: {
        message: "chmod +x app/vpipe/vpipe"
      }
    },
    {
      // Web app Python runtime (FastAPI + uvicorn + PyAV for input video
      // metadata — PyAV ships its own FFmpeg libraries, so no system
      // ffprobe needed). Model weights are NOT downloaded here — vpipe
      // fetches them lazily in the web UI, so a fresh install stays fast
      // and the user picks which variant to fetch.
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "uv pip install -r requirements.txt"
        ]
      }
    },
    {
      method: "fs.link",
      params: {
        venv: "app/env"
      }
    }
  ]
}
