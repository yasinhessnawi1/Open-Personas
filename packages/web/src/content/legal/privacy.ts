import type { LegalDocument } from "./types";

/**
 * The Privacy Policy for the hosted Open Persona service at openpersonas.si.
 *
 * Every product claim here must be true of the code and config. MAINTAINING.md lists
 * the files each section depends on. Text marked [[OWNER: ...]] is a fact only the
 * owner can supply; it stays visible on purpose until it is filled in.
 */
export const PRIVACY_POLICY: LegalDocument = {
  title: "Privacy Policy",
  version: "1.0.0",
  lastUpdated: "2026-11-18",
  intro: `This policy explains what personal data Open Persona collects, why, who else gets it, how long we keep it, and how you can see, change or delete it. It covers the hosted service at openpersonas.si. The [Terms of Service](/legal/terms) cover how you may use the service.

If you run the open source Open Persona software yourself, this policy does not cover that install. Whoever runs it decides what happens to the data.`,
  sections: [
    {
      id: "controller",
      heading: "Who is responsible for your data",
      body: `The controller of your personal data is [[OWNER: legal entity name]], a business registered in Norway, at [[OWNER: registered address]] ("we", "us").

For any question about your data, or to use any of your rights, write to [[OWNER: privacy contact email]]. [[OWNER: name a data protection officer here if one is appointed, or remove this sentence.]]`,
    },
    {
      id: "what-we-collect",
      heading: "What we collect",
      body: `**Your account.** Your email address, your password (held by our sign-in provider, Clerk, never by us in readable form), and the first and last name you give us.

**What you create and say.** Everything you put into Open Persona and everything your personas produce for you:

- your personas: their names, descriptions, instructions and settings;
- your conversations: the messages you write and the replies you get;
- your personas' memory: facts, events and summaries they keep from your conversations, the links between them, and earlier versions of a memory you corrected. The facts and links are shared between your own personas, never with other users;
- files you upload and files your personas create. We remove the location and camera data (EXIF) from images you upload;
- tasks, schedules and reminders, the results of runs, and the approvals you give or refuse;
- the services you connect, and the messages a persona reads or sends through them.

**Voice.** When you use voice, your speech is turned into text and the persona's reply is turned into speech. We keep the text of what was said, not the recording. See "Voice" below.

**Payments.** If you buy a plan or a credit pack: your name, email, billing address, the plan you are on, your payments and invoices. Your card details go straight to Stripe. We never see or store your full card number.

**Usage.** For each turn and task: the time, which model answered, how much text it read and wrote, and what it cost. We use this to charge credits correctly and to show you your usage in Settings.

**Activity records.** A record of actions in your account, such as a memory changed, a tool used or an approval given, with the time. They let you and us see what happened and when.

**Technical data.** Our servers log requests (time, IP address, the path requested, errors) to keep the service secure and working. Our own performance measurements record the route, the status and the time taken, without your account.

**A wellbeing marker.** If you tell a persona about something in a sensitive area (see "Wellbeing and safety"), we record that a disclosure happened in that conversation: your account, the persona, the conversation and the time. The marker holds no category and no text. A message caught by the crisis check (see "Wellbeing and safety") does not create a marker on its own. Separately, a memory in a sensitive area is labelled with its area so the wellbeing rules can apply to it.`,
    },
    {
      id: "how-we-use",
      heading: "Why we use it, and on what legal basis",
      body: `We only use your data to run Open Persona for you, to charge for it, and to keep it safe. The GDPR legal bases are:

- **To provide the service you signed up for** (performance of a contract, GDPR Article 6(1)(b)): your account, your personas, conversations, memory, files, tasks, connected services, voice and usage data.
- **To meet legal duties** (legal obligation, Article 6(1)(c)): keeping invoices and payment records under Norwegian bookkeeping law.
- **To keep the service secure and working** (legitimate interests, Article 6(1)(f)): request logs, abuse prevention, and the audit records that show what changed in your account and when. Our interest is a service that works and is safe; you can object (see "Your rights").
- **Sensitive information you choose to share** (Article 9): you may tell a persona about your health or other special categories of data. We process it on the basis of your explicit consent (Article 9(2)(a)), which you give by choosing to share it, and only to give you the service you asked for. The wellbeing rules below limit what a persona does with it. You can withdraw that consent at any time by forgetting the memory or deleting the persona. [[OWNER: the app shows no consent statement at the point where a user shares sensitive information. Decide whether to add one, and have a lawyer confirm Article 9(2)(a) is the right condition.]]

We do not sell your data. We do not use it for advertising. We do not use your content to train AI models.

**No automated decisions about you** (Article 22). We make no decisions with legal or similarly significant effects about you by automated means.`,
    },
    {
      id: "ai-providers",
      heading: "How your conversations reach the AI models",
      body: `A persona's replies are written by large language models run by other companies. To write a reply, we send the model what it needs for that turn: the persona's instructions, the recent conversation, memories that are relevant to it, the results of any tools it used, and any image or part of a file you shared that the reply needs.

Models are also called for other work the service does for you, and each call gets only the text that work needs:

- building and updating your personas' memory from your conversations;
- reading text out of files you upload;
- writing and refreshing conversation titles;
- making a persona's avatar picture;
- running your tasks, and preparing the actions you are asked to approve.

- We send these requests through **OpenRouter**, which passes them to the company that runs the chosen model (for example Anthropic, Google, OpenAI, DeepSeek, Moonshot, Z.ai, Qwen or others). Which company serves a turn depends on the model and on availability.
- On paid plans, some requests go directly to **NVIDIA** (NVIDIA NIM) instead.
- We do not send your email address or your account id with these requests. The persona's instructions can include the name you gave us, so it can address you by it.
- **Free models.** The free plan uses free models offered through OpenRouter. The companies that offer free models may keep prompts and use them to improve their models, under their own terms.
- Each provider handles the data under its own terms. We do not use your content to train models ourselves, and we do not give it to anyone for that purpose. [[OWNER: decide whether to set OpenRouter's data policy so that only providers which do not store or train on prompts are used. Until it is set, this paragraph must not promise more.]]

Memory search runs on our own servers. The models that find relevant memories, and the one that spots signs of a crisis, run on our own servers too, and your text does not leave them for those steps.`,
    },
    {
      id: "voice",
      heading: "Voice",
      body: `Voice runs on our own voice server in the EU, which streams the audio of a call over LiveKit.

- **Speech to text.** Your audio is streamed to a speech recognition provider, which returns the text. [[OWNER: name the production speech to text provider (the code default is Deepgram; Gladia is the other option) and confirm where it processes audio.]]
- **Text to speech.** The persona's reply text is sent to **ElevenLabs**, which returns the audio. Open Persona also supports **Cartesia**, which we do not use in production today. If we start using it, it will get the same reply text and we will add it here.
- We keep the transcript of a call as part of the conversation. We do not keep the audio recording.

[[OWNER: voice is not yet live on the hosted service. Confirm this section, and the voice host (planned: Microsoft Azure, Germany West Central), when it launches.]]`,
    },
    {
      id: "sharing",
      heading: "Who else gets your data",
      body: `We use these companies (our processors) to run the service. Each gets only what it needs for its job.

| Company | What it does for us | What it gets | Where |
| --- | --- | --- | --- |
| Oracle Cloud | Hosts our servers, database, files and nightly backups | Everything stored in Open Persona | EU (Frankfurt, Germany) |
| Vercel | Hosts the web app | Your requests to the web app, including your IP address | The app runs in Frankfurt, Germany; pages are delivered through Vercel's worldwide network |
| Clerk | Sign-in and account security | Your email and password at sign-up, sign-in sessions and devices, and a name only if you enter one in Clerk's profile screen | United States [[OWNER: confirm]] |
| Stripe | Payments, invoices and tax | Name, email, billing address, card details, payments | [[OWNER: confirm]] |
| OpenRouter | Routes requests to AI models | The content of each model request (see above) | United States |
| AI model companies, through OpenRouter | Write the replies | The content of each model request | Mostly United States, some elsewhere |
| NVIDIA | Writes replies on paid plans | The content of each model request | [[OWNER: confirm]] |
| [[OWNER: list any of these the production config enables, or remove this row: Anthropic, DeepSeek, Groq, Cloudflare Workers AI, a custom model provider]] | Write replies when a model is called directly instead of through OpenRouter | The content of each model request | [[OWNER: confirm]] |
| ElevenLabs | Turns replies into speech, when you use voice | The text of the persona's spoken replies | [[OWNER: confirm]] |

**Only when you use them.** These get data only if you turn the feature on or ask for it:

- **Messaging and email you connect**: Telegram, Discord and Slack get the messages a persona sends there and send us the messages you write there. For the email channel, Postmark carries the emails in both directions (sender, recipient, subject, text).
- **Web search**: when a persona searches the web, the search words it chose go to Brave Search. [[OWNER: confirm Brave or Tavily is the production search provider.]]
- **Web pages**: when a persona reads a web page, our server fetches that page from its website.
- **Running code**: when a persona runs code, the code and the files it needs go to E2B, which runs it in an isolated sandbox. [[OWNER: confirm E2B is enabled in production.]]
- **Image generation**: your image prompt goes to the image model, through OpenRouter. [[OWNER: confirm the production image provider.]]
- **Tools you add**: if you connect a tool or server, it gets what the persona sends it. For example, if you connect GitHub (you sign in to GitHub to allow it), GitHub gets the requests the persona makes there.

**Loaded by your browser.** When you open a PDF in the app, your browser loads the PDF viewer from unpkg.com, which sees your IP address.

**Others.** We share data with authorities only when the law requires it. If the business is sold or merged, your data would move with it under this policy, and we would tell you first.`,
    },
    {
      id: "transfers",
      heading: "Data sent outside the EEA",
      body: `Our servers and database are in the EU. Some processors above are based in the United States or reach the US, mainly the AI model companies, OpenRouter and the voice providers. When data goes to a country without an EU adequacy decision, the transfer relies on the European Commission's Standard Contractual Clauses, or on the EU-US Data Privacy Framework where the company is certified under it.

[[OWNER: confirm for each US processor which mechanism applies (Data Privacy Framework certification or Standard Contractual Clauses in its data processing agreement), and that a data processing agreement is in place with each.]]

You can ask us for a copy of the safeguards that apply.`,
    },
    {
      id: "retention",
      heading: "How long we keep it",
      body: `| Data | How long |
| --- | --- |
| Your account, personas, conversations, memory, files, tasks and connected services | Until you delete them, or delete your account |
| A memory you tell a persona to forget | Removed when you forget it |
| Per-turn usage records | Until you delete the conversation, the persona or your account |
| Activity records | Until you delete your account. After that they are kept with no end date, without their content and with your account id replaced |
| The wellbeing marker | Until you delete the persona or your account |
| Server request logs | No fixed period. They are rotated by size (at most 250 MB per service, and 500 MB of system log), so older entries are overwritten as new ones arrive |
| Nightly backups, and the database copy made before each update | About 14 days, then deleted |
| Invoices and payment records | At least 5 years from the end of the financial year they belong to, as Norwegian bookkeeping law requires, even after you delete your account |
| The record that a deletion happened | Kept, so a deleted account cannot be used again by mistake. It holds ids, dates and counts, not your content |`,
    },
    {
      id: "deletion",
      heading: "Deleting your data",
      body: `**Correct or forget a memory.** You can open your personas' memory, see what they keep, correct a memory, or forget it. Forgetting removes the memory, anything merged into it, and the conversation memory it came from; you see what will go before you confirm. A forgotten memory is removed, not hidden.

**Delete a conversation.** Deleting a conversation removes its messages. You can also tick the option to forget the conversation's memory. That also removes the facts a persona learned only from that conversation, and removes the conversation as a source of facts it also learned elsewhere; the dialog shows how many facts go before you confirm. A fact kept because you also said it elsewhere can still contain wording from the deleted conversation. Correct or forget it in Memory to remove that too.

**Delete a persona.** Deleting a persona removes it at once, with its conversations, its files, its tasks, its wellbeing markers and the memories that came only from it. Memories your other personas also hold stay with them, without the deleted persona's part. Activity records about it (without their content) stay until you delete your account. It cannot be undone.

**Delete your account.** You can delete your account in Settings, or delete it from the account screen of our sign-in provider; either way starts the same deletion. It happens at once, with no grace period, and removes every persona and everything in it. Your plan is cancelled and your saved cards are removed. If a step fails, the deletion keeps retrying until it finishes.

**What we keep after you delete your account.**

- Invoices and payment records, for at least the 5 years Norwegian bookkeeping law requires. We replace your account id in them with a pseudonymous deletion id. They still hold the Stripe customer id, and Stripe's customer record still holds your name and email. They hold amounts, dates and payment ids, never your conversations or personas.
- Stripe keeps its own copy of your invoices and its customer record (name, email, billing address) under its own retention rules.
- Activity records, with your account id replaced and their content removed, with no end date.
- The record that the deletion happened (ids, dates and counts).

**Backups.** Deleted data can stay in our nightly backups, and in the database copy we make before each update, for about 14 days, until those copies are deleted. Backups are only used to recover the service after a failure. [[OWNER: the decision is to re-apply every deletion after a restore from an off-site list; that list is not built yet. Add the sentence "If we restore a backup, we delete again everything that was deleted after it was made" once it is.]]`,
    },
    {
      id: "wellbeing",
      heading: "Wellbeing and safety",
      body: `People sometimes tell a persona about hard things. Open Persona has rules for that.

- **Sensitive areas.** The areas we treat as sensitive are: self harm and suicide, eating disorders, a mental health crisis, abuse and violence, and addiction. Serious illness, grief and ordinary health information are not treated this way.
- **Care with those memories.** A memory in a sensitive area is labelled with its area. A persona's memory never keeps details about means or methods of harm.
- **No initiative after a disclosure.** A persona never starts something on its own (a message or a suggestion you did not ask for) that draws on a memory in a sensitive area. And once you have shared something in a sensitive area in a conversation, the persona takes no initiative in that conversation. This stays in place even if you delete that memory, because deleting it is not a sign that it is fine to bring it up. Only deleting the persona or your account removes the marker. You can also tell us, from the conversation's menu, that it is fine for the persona to bring that conversation up again: it may then draw on the memories it learned there when it reaches out to you, but never on a memory in a sensitive area, on the conversation's own messages or on tasks you started there, and sharing something sensitive there again holds it back again. You can undo this at any time. A message caught by the crisis check below does not, on its own, turn this on.
- **Crisis.** If a message shows clear signs of a crisis, the persona does not answer in character. Instead you get a fixed message that says it is an AI and points you to a crisis line (findahelpline.com lists one for your country) and to your local emergency number. Softer signs of distress tell the persona to answer gently and put your safety first. This check runs on our own servers. It cannot catch every case.

Open Persona is not a medical or emergency service. If you are in danger, contact your local emergency number.`,
    },
    {
      id: "rights",
      heading: "Your rights",
      body: `Under the GDPR you have the right to:

- **access** your data and get a copy of it;
- **correct** data that is wrong. You can edit your profile and your personas yourself, and correct or delete what a persona remembers;
- **delete** your data (see "Deleting your data");
- **take your data with you** (portability): a copy of the data you gave us in a common machine readable format. There is no export button yet: email [[OWNER: privacy contact email]] and we send it within one month, as the GDPR requires.
- **object** to processing we do on the basis of our legitimate interests;
- **restrict** processing while a question about your data is being settled. Email us and we answer within one month;
- **withdraw consent** at any time, where we rely on it, without affecting what was done before.

To use any right, write to [[OWNER: privacy contact email]]. We answer within one month, and tell you if we need more time. We may ask you to confirm it is your account.

**Complaints.** You can complain to Datatilsynet, the Norwegian Data Protection Authority (datatilsynet.no). You can also complain to the data protection authority in the EEA country where you live or work. We would like the chance to sort it out first, so please write to us.`,
    },
    {
      id: "cookies",
      heading: "Cookies and browser storage",
      body: `We use only what the app needs to work:

- **Sign-in cookies** set by Clerk to keep you signed in and to protect your account.
- **A language cookie** that remembers the language you picked.
- **Browser storage** on your device that remembers display choices (theme, sidebar, how a file is shown), your recent searches in the command menu, and your most recent notifications so they show without a reload. It stays in your browser, and clearing the site's data in your browser removes it.

We do not use advertising or analytics cookies, and we do not track you across other websites. [[OWNER: confirm no analytics are added on the marketing site at openpersonas.si, which is a separate site.]]`,
    },
    {
      id: "security",
      heading: "How we protect it",
      body: `Your data is stored on our own servers in the EU. Connections are encrypted with TLS. Access to the servers is limited to the people who run the service. Each request is checked against your account, and the database enforces row level security, so one user cannot read another's data. Credentials for tools you connect are stored encrypted. Secrets such as service keys are kept out of the code and away from the tools a persona runs.

No system is perfectly secure. If a breach puts your data at risk, we will tell you and the supervisory authority as the law requires.`,
    },
    {
      id: "children",
      heading: "Children",
      body: `Open Persona is not meant for children. You must be at least [[OWNER: minimum age]] years old to create an account. If you think a child has given us personal data, write to us and we will delete it.`,
    },
    {
      id: "changes",
      heading: "Changes to this policy",
      body: `We update this policy when the product or the law changes. Each version has a number and a date at the top of this page. If a change matters for how your data is used, we will tell you before it takes effect.`,
    },
  ],
};
