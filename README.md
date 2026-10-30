# Split Pro for Home Assistant

Add expenses to [Split Pro](https://github.com/oss-apps/split-pro) groups (or with a friend) from
Home Assistant, and optionally track your balances as sensors.

It uses Split Pro's REST API (`/api/v1`), which isn't in a Split Pro release yet: it's upstream
pull request [oss-apps/split-pro#784](https://github.com/oss-apps/split-pro/pull/784), available
now on the [`alex-hacks`](https://github.com/alexm-forks/split-pro/tree/alex-hacks) build, where API
keys come from the server's `API_KEYS` setting.

## Install

1. In HACS, add this repository as a custom repository (type **Integration**) and install
   **Split Pro**, or copy `custom_components/splitpro` into your `config/custom_components`.
2. Restart Home Assistant.

## Set up

1. On the Split Pro server, add an API key for your user, then restart Split Pro:

   ```
   API_KEYS=you@example.com:<long random secret>
   ```

   (Generate one with `openssl rand -base64 32`.)
2. In Home Assistant: **Settings → Devices & services → Add integration → Split Pro**. Enter
   Split Pro's URL (e.g. `https://splitpro.example.com` or `http://192.168.1.10:3000`), the
   secret, and whether to verify the SSL certificate (turn off only for a self-signed one).
3. Choose which balance sensors to create: none are on by default. You can change this later
   under the integration's **Configure**.

Each group and friend appears as a device, whether or not it has a sensor, so actions can
target it.

## Sensors (optional)

- **Total balance**: your net balance across everyone.
- **Group balance** (per chosen group): your net balance in that group, with each member's
  balance as an attribute.
- **Friend balance** (per chosen friend): your net balance with them across groups and
  non-group expenses.

Positive means you're owed; negative means you owe. A sensor's unit is the currency, and it's
**unavailable** while its balance is in more than one currency. Sensors update every 5 minutes,
straight after the integration adds an expense, and when you call `splitpro.refresh`.

## Actions

### `splitpro.add_expense`

Target one group or friend device. Returns the expense id and each person's share.

| Field | | |
| --- | --- | --- |
| `description` | required | What it was for. |
| `amount` | required | The total, e.g. `42.50`. |
| `currency` | | Defaults to the group's default currency, then yours, then GBP. |
| `paid_by` | | Name, email or user id. Defaults to you. |
| `split` | | `default` (the group's or friend's saved default split, else equal), `equal`, `percentage`, `shares` or `exact`. |
| `participants` | | For `equal`: who's included. Defaults to everyone. |
| `shares` | | For `percentage`, `shares` or `exact`: each person and their value. People left out pay nothing. |
| `category` | | A Split Pro category, default `general`. |
| `date` | | Defaults to now. A date alone means midday that day. |

```yaml
action: splitpro.add_expense
target:
  device_id: <the group's device>
data:
  description: Groceries
  amount: 42.50
  category: groceries
response_variable: expense
```

Split 60/40 and say who paid:

```yaml
action: splitpro.add_expense
target:
  device_id: <the group's device>
data:
  description: Electricity
  amount: 120
  paid_by: Sam
  split: percentage
  shares:
    Alex: 60
    Sam: 40
```

### `splitpro.settle_up`

Records a payment. For a friend, `recipient` defaults to the other person; in a group, name the
`recipient`. `payer` defaults to you.

```yaml
action: splitpro.settle_up
target:
  device_id: <the friend's device>
data:
  amount: 15
```

### `splitpro.refresh`

Updates the balance sensors now.

## Notes

- Split Pro doesn't check the split it's sent, so the integration does the maths itself (a port
  of Split Pro's own) and rejects splits that don't add up.
- Amounts are converted to the currency's smallest unit (pence for GBP; none for JPY), and an
  amount with too many decimal places is rejected rather than rounded.
- The API key acts as your user, with your permissions: you can only add expenses to groups
  you're in.

## Development

```
pip install -r requirements_test.txt
pytest tests
```
