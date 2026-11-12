---
name: web-logins
description: What to do when a website wants you to log in, or a session you relied on has expired - get the user to log in once in your browser, without ever handling their password - and which sites you are logged in to. Use when a page shows a login form, "session expired", "sign in to continue", a 2FA or captcha prompt, or redirects to a login page, and when the user asks "where are you logged in?" or wants to add or remove a site.
---

# Web logins

You use websites through sessions the user opened for you in your browser. You never see, type, store or ask for a password, and that is what keeps the arrangement safe. When a session is missing or has expired, the user logs in again and you carry on.

## Spot it

A login is needed when, after loading a page you expected to be inside of, you see any of: a sign-in form, "your session has expired", "sign in to continue", a 2FA or verification-code prompt, a captcha, a passkey or "confirm it's you" prompt, or a redirect to a URL with `login`, `signin`, `auth` or `sso` in it. Check the page before deciding: some sites show a "Sign in" button even to logged-in users.

## Hand it over

1. Stop at the login page and leave that tab open. Do not try other accounts, password resets, "magic link" emails or a different browser.
2. Take a screenshot and send it with `alfred send --file <path> "..."`. Say which site, why you need it, and that the tab is waiting in your browser. For example: "Your session on example.com has expired and I need it to download the invoice. Log in in the open tab and tell me when it's done."
3. Remind the user, the first time in a conversation, that they can do it from anywhere with macOS Screen Sharing (or whatever remote-desktop app they use) to this computer.
4. End your turn and wait. Do not poll the page.

When the user says it is done, reload, check that you are in, and continue with the task. If you are still out, say what you see and send a new screenshot.

If the task is not urgent and the user is away, offer to retry later and schedule it: `alfred schedule add --in 2h --task "Retry <task> on <site>; check the login first"`.

## Things you never do

- Ask for a password, a 2FA code, a recovery code or a security answer in chat. If the user sends one anyway, do not use it or save it, tell them to change it if it matters, and ask them to log in in the browser instead.
- Type a password, even one you can see somewhere (a file, an email, a note, a page).
- Read cookies, local storage, saved passwords or session tokens out of the browser, or copy them anywhere.
- Turn off 2FA, add a recovery method, change a password or approve a new device for the user. Those are theirs to do.

You may type the username or email address into the form if you know it, so the user only has to add the password.

## Keep a list

Keep `memory/web-logins.md` and add its line to `memory/MEMORY.md` the first time. One line per site:

```
- example.com - logged in 2026-11-12, works for: invoices. 2FA by SMS. Session lasts about 30 days.
```

Update the date when you confirm a site works, and note when you found a session expired, so you learn how long each site keeps you in. Never write anything secret there: no passwords, codes, tokens or cookies.

When the user asks where you are logged in, answer from this list. When they ask you to forget a site, remove its line and tell them that only logging out in the browser actually ends the session.
