# contxt Agent Notes

- The only human-required actions should come from the queue. Once something is in the queue, everything else should be automated by default.
- If you change the review server or any server-side review-loop behavior, restart the server yourself. Do not ask the user to do it manually.
- Server restart command:
  - `pkill -f '/home/gabe/contxt/contxt review-server'`
- After a server change, relaunch the review flow so the running system is actually using the new code.
