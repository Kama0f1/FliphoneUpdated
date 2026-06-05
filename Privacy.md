\# Privacy Policy for Fliphone

\*\*Last Updated: June 5, 2026\*\*



\### 1. Introduction

Fliphone is a cross-server communication tool. We are committed to transparency regarding the data we collect and how it is used. By using Fliphone, you agree to the practices described in this policy.



\### 2. Data Collection (What is Logged)

To provide the core calling functionality and ensure community safety, Fliphone stores minimal metadata in a private database:

\*   \*\*Discord Identifiers:\*\* Server IDs, Channel IDs, and User IDs (specifically for administrators who configure the bot and users on the global ban list).

\*   \*\*Relay Data:\*\* Webhook URLs created during the setup process to facilitate message relay.

\*   \*\*Call Metadata:\*\* Start/end times, call duration, and the total number of messages sent per session (used for global statistics).

\*   \*\*Safety Reports:\*\* When a user reports a GIF, the specific \*\*GIF URL\*\* is logged for manual review, blacklisting, or whitelisting by the developer.



\### 3. Data We DO NOT Collect

\*   \*\*Message Content:\*\* Fliphone acts as a real-time relay. \*\*We do not log, store, or archive the text content of your conversations.\*\* Once a message is delivered to the partner server, it is not retained on our infrastructure.

\*   \*\*Personal Information:\*\* We do not collect emails, passwords, or real-world identity data.



\### 4. Data Storage and Security

\*   \*\*Infrastructure:\*\* All data is stored in a private database, either SQLite for local fallback or managed PostgreSQL for production hosting.

\*   \*\*Access:\*\* Access to the database is strictly restricted to the lead bot developer for technical maintenance and safety enforcement.

\*   \*\*Third Parties:\*\* We do not share, sell, or trade any logged data with third parties.



\### 5. User Control (Opt-In/Opt-Out)

\*   \*\*Opt-In:\*\* The bot is inactive until an administrator runs the `f.setup` command.

\*   \*\*Opt-Out:\*\* Administrators can run `f.teardown` at any time to immediately stop all relays and \*\*permanently delete\*\* the server's configuration and webhook data from our database.

\*   \*\*Manual Requests:\*\* Users may contact the developer via the support server to request the removal of any specific metadata associated with their User ID.



\### 6. Contact

If you have questions regarding this policy, please join our official support server.
