# Ubuntu Deployment Target

Agent Bridge will be hosted on the operator's Ubuntu server.

## Target host

- Host: `<YOUR_UBUNTU_SERVER_TAILSCALE_IP>`
- User: `<DEPLOY_USER>`
- Access: Tailscale/private network
- Role: central Agent Bridge hub and dashboard host

## Deployment intent

Agent Bridge is a standalone service, separate from ClawDeck, but it will initially live on the same Ubuntu server.

```text
Agents / connectors
        ↕
Ubuntu server: Agent Bridge hub/API
        ↕
Ubuntu server: Agent Bridge dashboard
        ↕
Admin browser
```

## Recommended paths

```text
/home/<DEPLOY_USER>/agent-bridge/
  hub/ or server/
  dashboard/
  data/
  files/
  logs/
  backups/
```

## Recommended services

```text
agent-bridge-hub.service
agent-bridge-dashboard.service
```

Later, if the OpenAgents base keeps a single workspace service, this can be simplified to one service. The important requirement is that the dashboard is served from the Ubuntu server, not only from a local dev machine.

## Initial network plan

Suggested initial private ports:

- Hub/API/WebSocket: `3010`
- Dashboard: `3011`

Final ports can change, but avoid colliding with ClawDeck:

- ClawDeck hub: `3000`
- ClawDeck dashboard: `3001`
- ClawDeck MQTT: `1883`

## Requirements

- Persistent room history survives restarts.
- Uploaded/shared files survive restarts.
- Dashboard can be opened from the admin machine over Tailscale.
- Agents connect to the Ubuntu hub from their own machines.
- Service restarts automatically after crashes/reboots.
- Backups should preserve `data/`, `files/`, and audit logs.

## Deployment principle

Do not treat Agent Bridge as a ClawDeck tab. It is a separate product/service. ClawDeck may link to it later, but Agent Bridge owns its own hub, dashboard, storage, and connector protocol.
