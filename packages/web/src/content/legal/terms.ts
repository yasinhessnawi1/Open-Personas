import type { LegalDocument } from "./types";

/**
 * The Terms of Service for the hosted Open Persona service at openpersonas.si.
 *
 * Every product claim here must be true of the code and config. MAINTAINING.md lists
 * the files each section depends on. Text marked [[OWNER: ...]] is a fact only the
 * owner can supply; it stays visible on purpose until it is filled in.
 */
export const TERMS_OF_SERVICE: LegalDocument = {
  title: "Terms of Service",
  version: "1.0.0",
  lastUpdated: "2026-11-18",
  intro: `These terms are the agreement between you and [[OWNER: legal entity name]] ("we", "us") for using Open Persona, the hosted service at openpersonas.si. Please read them with the [Privacy Policy](/legal/privacy), which explains what we do with your data.

If you run the open source Open Persona software on your own machine, these terms do not apply to that install. The software license that comes with the code does.`,
  sections: [
    {
      id: "who-we-are",
      heading: "Who we are",
      body: `Open Persona is run by [[OWNER: legal entity name]], a business registered in Norway, at [[OWNER: registered address]], organisation number [[OWNER: organisation number]].

You can reach us at [[OWNER: contact email]].`,
    },
    {
      id: "your-account",
      heading: "Your account",
      body: `- You need an account to use Open Persona. You sign up with an email address and a password, and you confirm the address with a code we send to it.
- You must be at least [[OWNER: minimum age]] years old to create an account.
- Keep your password to yourself. You are responsible for what happens under your account, including what your personas do on your behalf.
- Give us a real email address and keep it current, so we can reach you about your account and your payments.`,
    },
    {
      id: "the-service",
      heading: "What Open Persona does",
      body: `Open Persona lets you create AI personas and talk with them in chat. A persona:

- **remembers.** It keeps a long-term memory of what you tell it and what happens in your conversations, so it can use that later. You can look at that memory, correct it and make it forget.
- **runs tasks.** You can ask it to do work, on demand or on a schedule. Some actions wait for your approval before they happen.
- **can take some initiative.** A persona can bring something up on its own, for example a follow-up on a task. It does not do this in a conversation where you shared something about your wellbeing (see the Privacy Policy).
- **can reach other services you connect.** If you connect a messaging app, an email address or another tool, the persona can read and send messages there within what you allowed.

The replies come from large language models run by other companies (listed in the Privacy Policy). We choose and route the models; we do not write the replies.`,
    },
    {
      id: "ai-limits",
      heading: "What AI can get wrong",
      body: `- **Replies can be wrong.** A persona can state things that are false, out of date or made up, and sound sure while doing it. Check anything that matters before you rely on it.
- **A persona is not a person.** It is software playing a character you set up. It has no feelings, duties or professional qualifications.
- **It is not professional advice.** Nothing a persona says is medical, legal, financial or other professional advice.
- **It is not an emergency service.** If you are in danger or thinking about harming yourself, contact your local emergency number or a crisis line now. If a conversation shows signs of a crisis, Open Persona steps out of the character and points you to real help, but it cannot call anyone for you.
- **Actions have effects.** When a persona sends a message, runs a task or uses a tool you connected, that really happens. Review what you approve.`,
    },
    {
      id: "your-content",
      heading: "Your content",
      body: `"Your content" means what you put into Open Persona (messages, files, persona descriptions, instructions) and what your personas produce for you (replies, memories, files).

- **It stays yours.** We claim no ownership of it.
- **What you let us do with it.** You give us permission to store, process and transmit your content only as needed to run the service for you: to show it to you, to send it to the models that write the replies, to build and use your persona's memory, to run your tasks, and to deliver messages through services you connected. This permission ends when you delete the content or your account, except for the backup window described in the Privacy Policy.
- **We do not train models on it.** We do not use your content to train AI models. The model providers' own handling is explained in the Privacy Policy.
- **You are responsible for what you put in.** Only upload content you have the right to use, and do not put other people's personal data into Open Persona unless you are allowed to.`,
    },
    {
      id: "acceptable-use",
      heading: "What you must not do",
      body: `Do not use Open Persona to:

- break the law, or help someone else break it;
- harass, threaten, impersonate or deceive people, including by sending messages through a connected service that pretend to come from someone else;
- create sexual content involving minors, or content that sexualises a real person without their consent;
- create or spread malware, or attack, probe or overload any system, including ours;
- send spam or unsolicited bulk messages through a connected service;
- get around our usage limits, billing or safety features, or reverse engineer the hosted service to do so;
- resell or give others access to your account.

You must also follow the rules of every service you connect (for example the terms of a messaging app). We may remove content or limit an account that breaks these rules, as described in "Ending your use".`,
    },
    {
      id: "credits-and-billing",
      heading: "Credits, plans and payment",
      body: `**Credits.** Open Persona is paid for with credits. Your balance and every price are shown in US dollars on the Billing page. Credits are not money: they cannot be withdrawn, transferred or exchanged for cash.

**How a call is charged.** Most things a persona does call a language model. Each call is charged at the price of the model that actually served it, for the amount of text it read and wrote. If a call fails and the model still used tokens (for example it returned an empty or broken answer), that attempt is charged too, at the price of the model that made it. A failed attempt that used no tokens costs nothing. Other services have their own price, such as image generation, voice and running code. The usage list in Settings shows what each turn cost.

**Plans.** There is a free plan and paid plans. Each plan gives you an amount of credit to spend every month. That monthly amount refreshes each month and what you do not use does not carry over. The current plans, their prices and their monthly amounts are on the Billing page. A paid plan renews every month until you cancel it.

**Credit packs.** You can buy a one-time credit pack at any time. Pack credit lasts for the number of months shown on the Billing page, counted from the day you buy it, and expires after that. Packs are spent oldest first.

**Auto top-up.** On plans that offer it, you can turn on auto top-up. When your balance drops below the amount shown on the Billing page, we charge your saved card for a pack so your personas keep working. You can turn it off at any time.

**Payment and tax.** Payments are handled by Stripe. We do not see or store your full card number. Stripe works out any VAT or sales tax from the billing address you give at checkout, and checkout shows it before you pay. [[OWNER: confirm whether the prices on the Billing page include or exclude VAT.]]

**When a payment fails.** If a renewal payment does not go through, your plan is put on hold and its monthly credit does not renew until the payment is sorted.

**If you run out.** When your balance runs out, you cannot use what costs credits until you add more or your monthly amount refreshes.`,
    },
    {
      id: "cancellation-and-refunds",
      heading: "Cancelling and refunds",
      body: `- **Cancelling a plan.** You can cancel a paid plan at any time from the Billing page (Manage subscription). It stays active until the end of the period you paid for, then it stops renewing.
- **Unused credit.** Unused monthly credit, and unused pack credit, is not refunded, except where the law requires it.
- **Deleting your account.** Deleting your account ends your plan at once. Unused days and unused prepaid credit are not refunded. If you want a refund, contact us before you delete your account.
- **Your right of withdrawal.** If you are a consumer in the EEA, the law may give you a right to withdraw from a purchase within 14 days. [[OWNER: confirm how the right of withdrawal applies to plans and credit packs, and whether checkout asks for consent to immediate performance.]]
- **Mistakes.** If we charged you in error, contact us and we will put it right.`,
    },
    {
      id: "connected-services",
      heading: "Services you connect",
      body: `Open Persona can connect to services run by others: messaging apps, email, tools you add, and the model providers that write the replies. We do not control those services. Their own terms and privacy policies apply to what happens there, and we are not responsible for them being available or for what they do with data you send them.`,
    },
    {
      id: "availability",
      heading: "Availability and changes to the service",
      body: `We work to keep Open Persona running, but we do not promise it will always be available, fast or free of errors. Models and providers change, and a model we use today may be replaced by another. We may change, add or remove features. If a change takes away something you paid for, we will tell you in advance where we can and give you a fair way out, such as cancelling before the change.`,
    },
    {
      id: "ending",
      heading: "Ending your use",
      body: `- **You** can stop at any time. You can delete a single persona in the app, or your whole account in Settings. Deletion is immediate and cannot be undone. The Privacy Policy explains what is removed and what we have to keep.
- **We** may suspend or close an account that seriously or repeatedly breaks these terms, that puts other people or the service at risk, or where the law requires it. Unless the law or the risk prevents it, we will tell you why first and give you a chance to respond. You can still ask for a copy of your data by email, as the Privacy Policy explains, and we send it within one month.`,
    },
    {
      id: "liability",
      heading: "Our responsibility to you",
      body: `- If you are a consumer, you keep every right the law gives you, and nothing in these terms takes those rights away.
- We are responsible for harm we cause on purpose or through gross negligence, and for anything else the law does not let us exclude.
- Apart from that, we are not responsible for loss caused by relying on what a persona said, by actions you approved or allowed a persona to take, or by services you connected.
- [[OWNER: any cap on liability for business users, to be checked by a lawyer.]]`,
    },
    {
      id: "law",
      heading: "Law and disputes",
      body: `These terms are governed by Norwegian law. If you are a consumer living in another country, you also keep the protection of the mandatory consumer laws of that country.

If something goes wrong, contact us first at [[OWNER: contact email]] and we will try to solve it. Disputes go to the Norwegian courts, [[OWNER: venue, for example Oslo District Court]], but as a consumer you can also bring a claim in the courts where you live. Consumers in Norway can also contact Forbrukerrådet (the Norwegian Consumer Council).`,
    },
    {
      id: "changes",
      heading: "Changes to these terms",
      body: `We may update these terms when the product or the law changes. Each version has a number and a date at the top of this page. If a change affects your rights or what you pay, we will tell you before it takes effect, and you can cancel and delete your account if you do not accept it.`,
    },
  ],
};
