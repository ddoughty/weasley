# Minimal TRMNL Visualization for Weasley Location Data

This guide shows the smallest possible TRMNL plugin setup that renders the location payload sent by Weasley (`trmnl.py`).

## 1) Required Weasley env vars

Set these in `.env`:

```dotenv
WEASLEY_TRMNL_API_KEY="<your-trmnl-api-key>"
WEASLEY_TRMNL_PLUGIN_UUID="<your-plugin-uuid>"
```

## 2) Create a private TRMNL plugin

1. In TRMNL, create a new private/custom plugin.
2. Copy the plugin UUID into `WEASLEY_TRMNL_PLUGIN_UUID`.
3. Use this Liquid template (minimal rendering):

```liquid
<div class="layout layout--col gap--space-between">
  <div>
    <div class="title">Weasley Clock</div>
    <div class="label">{{ member_count }} member{% if member_count != 1 %}s{% endif %}</div>
  </div>

  {% if members and members.size > 0 %}
    <div class="layout layout--col gap--small">
      {% for member in members %}
        <div>
          <div><strong>{{ member.name }}</strong> - {{ member.location_label | default: "Unknown" }}</div>
          <div class="label">{{ member.last_seen | default: "Unknown" }} | {{ member.battery_level | default: "?" }}{% if member.battery_status %} ({{ member.battery_status }}){% endif %}</div>
        </div>
      {% endfor %}
    </div>
  {% else %}
    <div>No member locations available.</div>
  {% endif %}

  <div class="label">Updated {{ updated_at }}</div>
</div>
```

## 3) Confirm payload shape (what Weasley sends)

Weasley posts JSON in this shape:

```json
{
  "merge_variables": {
    "members": [
      {
        "name": "Molly",
        "lat": 39.7736,
        "lon": -75.5933,
        "battery_level": "83%",
        "battery_status": "Charging",
        "last_seen": "04:12 PM",
        "location_label": "Home"
      }
    ],
    "updated_at": "04:15 PM",
    "member_count": 1
  }
}
```

The template fields above map directly to `merge_variables.members`, `merge_variables.updated_at`, and `merge_variables.member_count`.

## 4) Send a manual test payload

Use this once to verify the plugin renders before wiring full Weasley runs:

```bash
curl -X POST "https://trmnl.com/api/custom_plugins/$WEASLEY_TRMNL_PLUGIN_UUID" \
  -H "Authorization: Bearer $WEASLEY_TRMNL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "merge_variables": {
      "members": [
        {
          "name": "Molly",
          "lat": 39.7736,
          "lon": -75.5933,
          "battery_level": "83%",
          "battery_status": "Charging",
          "last_seen": "04:12 PM",
          "location_label": "Home"
        }
      ],
      "updated_at": "04:15 PM",
      "member_count": 1
    }
  }'
```

## 5) Push real data from Weasley

After auth/config are done, run:

```bash
python main.py once
```

If TRMNL credentials are set, Weasley will call the same webhook and refresh the display.
