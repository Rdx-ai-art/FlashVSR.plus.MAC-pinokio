module.exports = {
  // daemon: true keeps the server process alive after the run array completes.
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        venv: "env",
        env: { },
        path: "app",
        message: [
          "python app.py --port {{port}}"
        ],
        on: [{
          // Capture the URL the web app prints when the server is up.
          // The regex match object is passed to the next step as `input.event`.
          "event": "/(http:\\/\\/[0-9.:]+)/",

          // done: true moves to the next step while keeping the server shell alive.
          "done": true
        }]
      }
    },
    {
      // Set the local 'url' variable from the captured match; pinokio.js uses it
      // to surface the "Open Web UI" tab.
      method: "local.set",
      params: {
        url: "{{input.event[1]}}"
      }
    }
  ]
}
