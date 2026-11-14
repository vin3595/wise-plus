# Testing wise-plus on the Wise server

wise-plus runs **next to Wise** on the same AWS server, at its own address (e.g. `plus.easemybill.com`),
with its own database and data folder (`/opt/wiseplus`). Wise keeps running as it is.

## 1. Point the address at the server (Cloudflare, 1 minute)

DNS → Add record: **Type** A · **Name** `plus` · **IPv4** the same IP as `pos` · **Proxy** off (grey cloud).

## 2. Install (on the server, 15–25 minutes the first time)

Connect to the server the way you do for Wise (EC2 → Connect → Session Manager), then:

```
sudo git clone -b emb-look-and-bill-scan https://github.com/vin3595/wise-plus /opt/wiseplus/src
sudo bash /opt/wiseplus/src/deploy/plus/install.sh
```

It asks for the address (Enter = plus.easemybill.com) and, optionally, your Gemini key for Scan bill.

## 3. Use it

Open https://plus.easemybill.com, sign up a test restaurant. The sign-in code (no SMS on the test app):
`sudo wiseplus-cloud login-code`

## Looking after it

| | |
|---|---|
| `sudo wiseplus-cloud status` | running?, address, memory, last backup |
| `sudo wiseplus-cloud update` | newest code from GitHub (backs up first) |
| `sudo wiseplus-cloud gemini <key>` | switch on Scan bill (`gemini off` to remove) |
| `sudo wiseplus-cloud signup closed` | stop new test restaurants being made |
| `sudo wiseplus-cloud logs api` | what went wrong |
| `sudo wiseplus-cloud remove` | stop it and take its address off Wise's server (data kept) |

## How it sits next to Wise

- Wise's Caddy already owns ports 80/443. wise-plus adds **one site block** at the end of Wise's
  Caddyfile, between `# wiseplus-begin` and `# wiseplus-end`. Before saving, Wise's own Caddy checks the
  new file; if it doesn't accept it, nothing changes. If the reload fails, the old file goes back.
- A Wise update replaces that Caddyfile; a timer puts the plus block back within 10 minutes.
- wise-plus's containers use names Wise doesn't (`plusweb`, `plus-api`), so Wise's traffic can't reach them.
- Memory: about 150 MB idle (API 115, database 25, web 4), capped at about 860 MB.
- If Wise isn't on the server, wise-plus runs its own Caddy instead.

Rehearsed in a copy of this setup (stand-in Wise Caddy and network, a decoy `api`): both addresses served
only their own app 20/20, a broken block was refused with Wise untouched, a Wise update was healed by the
timer, and `remove` left Wise running.
