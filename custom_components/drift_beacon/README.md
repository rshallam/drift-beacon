# Drift Beacon Integration

## Activity controls blueprint

Use `blueprints/drift_beacon_activity_button.yaml` (displayed as **Drift Beacon
Activity Controls**) to map activity actions to any Home Assistant trigger.
Requires Home Assistant 2024.10 or later.

Choose the activity once using its Session switch, Pin switch, or point-activity
button. The picker uses the existing entity names without adding an activity-type
suffix. Do not choose the workspace's Stop session button.

- **Track now:** Track toggles a timed activity or marks a point activity. Pause
  ends the selected activity's live timed session and pins it for later; it does
  nothing for point activities or when another activity is live.
- **Plan ahead:** Pin, Queue, and Unpin have independent optional trigger mappings.
  Pin moves any previous pin to the queue front. Queue adds an entry at the back,
  or pins immediately when auto-advance is enabled and nothing is pinned. Repeated
  queue triggers can add multiple entries. Unpin can advance the queue.
- **Current session controls:** Stop or pause the connected user's live session,
  whichever activity it belongs to. Ending a shared session ends it for all members.

Every mapping starts empty. Add at least one trigger. Overlapping device events
can run multiple mappings; choose the device events you intend to use.

This replaces the gesture-based Press / Double press / Hold inputs. Reconfigure
those mappings when importing the updated blueprint. Install the updated
integration and restart Home Assistant before using the new activity services.

## Activity services

`track_activity`, `pause_activity`, `pin_activity`, `queue_activity`, and
`unpin_activity` accept explicit Drift Beacon activity entities as `entity_id`
targets. Activity identity comes from the entity registry, so renaming an entity
or selecting its Pin switch does not change which activity is controlled.
Selecting multiple entities for the same activity applies the action only once.

`stop_session` remains the workspace/current-session action. Set `pause: true`
to end and pin the current activity instead of only ending its session.
