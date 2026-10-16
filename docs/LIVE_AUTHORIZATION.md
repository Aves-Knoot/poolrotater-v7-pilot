# New-host OAuth authorization only

Existing upgraded hosts retain OAuth and do not run these steps again.
Use your own osu! OAuth client and your own hosting account; keep the client
secret and token files on your machine. The maintainer never needs them.

Set pilot_instance and pilot_release from the fresh install result. On the
operator machine, use the private terminal helper (which prompts locally):

```bash
python3.12 -I -S -B configure_live.py --instance "$pilot_instance" \
  --release "$pilot_release" --expected-user-id YOUR_OWN_OSU_USER_ID \
  --redirect-uri http://127.0.0.1:51111/oauth/callback
```

On your browser machine open the SSH tunnel to your operator machine:

```bash
ssh -N -L 127.0.0.1:51111:127.0.0.1:51111 LOGIN@OPERATOR_HOST
```

After that tunnel is established, on the operator machine run:

```bash
sudo /usr/bin/python3.12 -I -S -B \
  "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance "$pilot_instance" authorize --operator-mode ssh
```

Open the printed URL locally. The callback listens only on127.0.0.1; no reverse
proxy, public listener or central OAuth credential forwarding is required.
Authorization verifies the account; starting rooms performs additional fresh
identity/client/scope/ownership checks. Authorization does not start rooms.
Never paste the authorization URL, code, state, client secret or token files into
public reports. If the listener is uncertain, retain state and inspect status
before beginning another authorization attempt.
