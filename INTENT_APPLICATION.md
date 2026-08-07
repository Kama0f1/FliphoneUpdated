# Fliphone Message Content Intent Application

Use this document as a copy and paste worksheet for the Discord Developer Portal.

## Application Details

Fliphone is a voluntary real time cross server conversation bot. A server administrator first chooses one text channel and configures it with `/setup`. Users can then start a random 1:1 call with `/call` or join a group room with `/room`.

Fliphone processes ordinary server messages only after confirming that the message was sent in an administrator configured Fliphone channel during an active call or room. It applies safety filters, link and attachment rules, GIF and custom emoji rules, and rate limits. Permitted content is delivered to participating channels through Discord webhooks. Messages in unrelated channels and messages outside active conversations are ignored before content is inspected.

Slash commands are Fliphone's primary control surface. Members may also use the `f.` prefix or mention Fliphone for supported command shortcuts. Restricted tools verify the caller against the bot owner and trusted moderator user ID list. Message Content is required primarily for the active conversational relay, not for commands.

Ordinary conversation text is not stored in PostgreSQL, SQLite, logs, or backups. When a user submits `/report`, Fliphone fetches the full eligible conversation window for the selected call or room from the reporting Discord channel, posts it as one or more text attachments in a private Discord moderation channel, and discards that context locally. The private evidence messages are scheduled for deletion when resolved or after 30 days. The database stores only report metadata, Discord evidence message IDs, and the written reason submitted through the report interaction.

Users can run `/privacy optout` at any time. Their future messages are ignored before content processing and are excluded from new report context. They can use `/privacy optin` to participate again.

## Privacy Policy Question

Select **Yes**. Use this public URL:

https://gist.github.com/Kama0f1/431f01bbbf1ae6243505778376ba0fb3

## Terms of Service URL

https://gist.github.com/Kama0f1/085bf38fb03e3c84d99f2ff9afc410af

## Intent Selection

Select only **Message Content Intent**.

Do not select Server Members Intent or Presence Intent unless the production code later adds a feature that cannot work without them.

## Can Users Opt Out

Select **Yes**.

Users run `/privacy optout`. Fliphone then ignores their future messages before reading content, does not relay them, and excludes them from new report context. `/privacy optin` enables participation again.

## Is Message Content Stored Outside Discord

Select **No** for ordinary server message content obtained through Message Content Intent.

Fliphone does not store ordinary conversation text in PostgreSQL, SQLite, logs, or backups. On demand report context is posted to a private Discord moderation channel and discarded locally. The database stores report metadata, Discord evidence message IDs, and the report reason entered through a Discord interaction, not a stored conversation transcript.

## Is Message Content Used for AI Training

Select **No**.

Fliphone does not use message content to train machine learning or artificial intelligence models.

## Why Message Content Intent Is Required

Fliphone's core feature is real time conversational relay between Discord channels. During an active call or room, users type ordinary messages naturally in the administrator configured Fliphone channel. The bot must receive the content of those messages to apply safety rules and relay permitted content to the connected Discord channels.

Discord webhooks only provide the delivery step. They allow Fliphone to post a relayed message into the destination channel, but they do not provide the original user's message to Fliphone. Without Message Content Intent, ordinary messages arrive with empty content and the conversation cannot be relayed.

Slash commands, buttons, modals, and message context commands are used for every feature that can reasonably use interactions, including setup, calling, leaving, privacy controls, submissions, reports, and moderation. Requiring users to submit every chat message through a slash command, modal, mention, or context menu would replace normal real time conversation with a separate interaction for every message and would make the core service unusable.

Access is narrowly limited in code. Fliphone first checks whether the channel belongs to an active call or room. Only then does it inspect content. It ignores unrelated channels, inactive configured channels, users who opted out, local only messages beginning with `x `, bot messages, and command interactions.

## Demonstration Links

Replace these placeholders before submission.

1. Full demonstration video: `[PASTE PUBLIC OR UNLISTED VIDEO LINK]`

2. Active call screenshot: `[PASTE SCREENSHOT LINK]`

3. Privacy opt out screenshot: `[PASTE SCREENSHOT LINK]`

4. Report workflow screenshot: `[PASTE SCREENSHOT LINK]`

## Video Recording Order

1. Show a server administrator running `/setup` in a dedicated Fliphone channel.

2. Show two servers connecting with `/call` or multiple servers joining `/room`.

3. Send ordinary messages and show them appearing through the destination webhook.

4. Send a message in an unrelated channel and show that Fliphone does nothing.

5. End the call, send another message in the configured channel, and show that Fliphone does nothing.

6. Run `/privacy optout`, start a conversation from another participating user, and show that the opted out user's message is not relayed.

7. Run `/report`, show the private report confirmation, and show the private moderator report with its full context attachment. Hide private server names, user IDs, webhook URLs, tokens, and unrelated report content before publishing the video.

8. Show `/sudohelp` from a trusted moderator account and show an unauthorized account being denied access to a restricted command.

## Submission Checklist

1. Update both public Gists with the final policy text from this repository.

2. Open both Gist links in a private browser window and confirm they are publicly readable.

3. Set the Privacy Policy URL and Terms of Service URL in the Discord Developer Portal application settings.

4. Deploy and test the current production commit before recording evidence.

5. Record the demonstration and upload it as an unlisted video or another publicly viewable link.

6. Add the video and screenshot links above.

7. Select only Message Content Intent in the application.

8. Paste the answers from this worksheet into the matching fields.

9. Review every answer for accuracy against the deployed build.

10. Submit the application within Discord's stated review window. Keep the existing intent enabled while the new application is under review.
