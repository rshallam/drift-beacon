# Drift Beacon integration

Connects Home Assistant to one Drift Beacon workspace, acting for the user who owns the
access token. Requires Home Assistant 2026.9 or later and a Drift Beacon server with the
`TrackActivity` RPC and `PointMarked` messages.

## Devices

- **Workspace device** (model `Workspace`): *Current session* and *Pinned activity* sensors,
  *Connected user* (diagnostic), and a *Stop session* button.
- **One device per activity** (model `Activity`, model id `span` or `point`), linked to the
  workspace device:
  - *Session* switch (timed activities): on while you are tracking it.
  - *Mark* button (point activities): records one occurrence.
  - *Pin* switch: on while it is your pinned activity.
  - *Progress* sensor: completed time or mark count for the activity's progress period,
    across all workspace members; the running session is not included.

Every entity is **hidden**, so hundreds of activities never appear on auto-generated
dashboards. Devices still show under Settings → Devices. Unhide an entity to use it on a
dashboard.

Archiving or deleting an activity removes its device. If the activity comes back, its device
returns with the same id, so action targets keep working; automations that use the device's
triggers need a reload (or a restart) before they attach again. Renaming an activity
renames its device unless you gave the device your own name.

## Actions

All actions take **devices** as targets. Entities are hidden, and Home Assistant skips hidden
entities when it expands a device target, so the integration resolves devices itself. Areas,
labels and entity targets are rejected.

| Action | Target | Does |
| --- | --- | --- |
| `drift_beacon.track_activity` | activity | Timed: start, or stop if already tracking. Point: mark. The server decides, so repeated presses never restart a session. |
| `drift_beacon.pause_activity` | timed activity | End its live session and pin it. Nothing live is a no-op. |
| `drift_beacon.pin_activity` | activity | Pin it; the previous pin moves to the front of the queue. |
| `drift_beacon.unpin_activity` | activity | Unpin it if pinned; with auto-advance on, the queue head is pinned. |
| `drift_beacon.queue_activity` | activity | Add it to the back of the queue (or pin it, with auto-advance on and nothing pinned). |
| `drift_beacon.stop_session` | workspace | Stop your live session, whichever activity it belongs to. |
| `drift_beacon.pause_session` | workspace | End your live session and pin its activity. |

Ending a shared session ends it for everyone in it.

## Triggers and events

Activity devices offer device triggers: *Tracking started*, *Tracking stopped* (timed),
*Marked* (point), *Pinned* and *Unpinned*.

The integration fires these bus events. All carry `workspace_id` and `workspace_device_id`;
activity events add `activity_id`, `activity_device_id`, `activity_name` and `color`
(`[r, g, b]` or `null`).

| Event | Extra data |
| --- | --- |
| `drift_beacon_session_started` | `session_id`, `started_at`, `member_ids` |
| `drift_beacon_session_stopped` | `session_id`, `started_at` |
| `drift_beacon_activity_pinned` | `pinned_at` |
| `drift_beacon_activity_unpinned` | |
| `drift_beacon_activity_marked` | `session_id`, `marked_at`, `member_ids` |
| `drift_beacon_focus_changed` | `state` (`live`, `pinned` or `idle`), `previous_state`, `previous_activity_id`, `initial` |

Focus is what you are doing right now: a live session beats a pin, which beats nothing. It is
computed after each server update, so switching straight from one activity to another is a
single `live` → `live` change. It is also announced once when Home Assistant starts
(`initial: true`), and after a reconnect if it changed while disconnected.

## Blueprints

- **Drift Beacon Activity Controls** (`drift_beacon_activity_controls.yaml`): pick an activity
  device, then map any triggers to Track, Pause, Pin, Queue and Unpin.
- **Drift Beacon Session Controls** (`drift_beacon_session_controls.yaml`): pick the workspace
  device, then map triggers to Stop or Pause your live session.
  Both controls blueprints work out which mapping fired from the trigger's position, which
  renders every mapped trigger. Triggers containing templates (template triggers, numeric
  state value templates) fail there, so put those in their own automation that calls the
  action directly.
- **Drift Beacon Activity Lighting** (`drift_beacon_activity_lighting.yaml`): colours lights
  from `drift_beacon_focus_changed` for the chosen workspace.

## Upgrading from 0.2.x

Activities used to be loose switches and buttons on the workspace device, and the blueprints
selected those entities. Entries from those versions cannot be migrated: remove the
workspace entry, add it again, and re-create automations from the updated blueprints.

## Connection

The config flow detects `https` or `http`, and verifies TLS certificates unless you turn
**Verify SSL certificate** off (needed for the add-on's self-signed certificate). Change the
address later with **Reconfigure**. If the token stops working, Home Assistant asks for a new
one for the same workspace and user.
