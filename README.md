# Nagisa

Nagisa is a companion service for Shoko, Plex, and Sonarr. It provides guarded anime ordering review and import reconciliation, with optional arrival and availability components. The service holds ambiguous identities and unknown dependency versions for review.

## Build

Build the container from this source tree:

```sh
docker build -t nagisa:local .
```

The HTTP UI listens on port 8787 and is intended for a trusted local network. Keep runtime state and credentials in a private `/config` mount. Do not commit real application settings, API keys, databases, logs, or media inventories.

The automated check compiles the Python package, checks the CLI, and builds the image. Integration and environment-specific behavior require separate validation before deployment.
