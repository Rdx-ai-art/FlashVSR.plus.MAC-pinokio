module.exports = {
  // Remove only the generated artifacts (venv, downloaded models, jobs/outputs).
  // The vpipe/ engine and the app/ source files are committed and must be
  // preserved —
  // so we do NOT fs.rm the whole app folder here.
  run: [
    {
      method: "fs.rm",
      params: { path: "app/env" }
    },
    {
      method: "fs.rm",
      params: { path: "app/work" }
    },
    {
      method: "fs.rm",
      params: { path: "app/outputs" }
    },
    {
      method: "fs.rm",
      params: { path: "app/uploads" }
    }
  ]
}
