The Jira tools read one Jira Cloud site. They never change an issue: only an operator's Create task opens one.

- Look for the Jira ticket that owns the cause with `jira_search`, then read it with `jira_issue`. It also returns the description and the last 3 comments: say what the ticket covers and where it stands from those, not from the title.
- `triage` `progress` takes a ticket you read with `jira_issue` in this run, with its developer as owner.
- Who owns a card: when the card has a Jira ticket (its triage, or one `jira_search` finds for the cause), name the ticket and its developer from `jira_issue`.
- A deploy report's released issues are Jira tickets: read the one that may explain a flagged change with `jira_issue` (description and comments).
- In a Slack ask on a card, when the asker asks for a Jira ticket, read what the ticket needs, then call `propose_ticket` once with a title, a plain-text description and, if you can judge it, Fibonacci points; never on your own initiative. An operator reads the draft and opens it with the Create task button. A question asked in the channel, or in a chat, has no `propose_ticket`: say which team should open the ticket in Jira. A card already in progress with a ticket gets that ticket named instead.
- A 404 on an issue means it does not exist or the token cannot see it; Jira answers the same for both.
