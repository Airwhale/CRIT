# CRIT — Product Brief

**A self-hosted, AI-powered game recommender that reasons about my actual play history across Steam, Epic, and GOG to provide informed game recommendations.**

## The problem

After a decade of Steam sales, Humble bundles, and Epic giveaways, I own hundreds of games across three stores and no good way to decide what to play next. Existing recommenders look at one storefront at a time, don't know what I've already finished, So I put together something that looks at my total library across three platforms, my steam playtime, and current deals, and reccomends me what I should play next from my current library or deep discount sales.

![CRIT hero view — library + streaming recommendation](docs/screenshots/hero.png)

## Who it's for

Hardcore PC gamers with libraries across two or three storefronts — the player who owns 200+ games, has scooped Epic freebies for years, and loses ten minutes an evening to "what should I play?" paralysis. Tech-literate enough to self-host and supply their own API keys.

## Job to be done

> *When I have 30–90 minutes to play tonight, help me pick a game I'll actually enjoy — whether I already own it, should buy it on sale, or should try something new.*

Low stakes per decision, high frequency. The winner isn't the fastest tool; it's the one trusted enough that users stop second-guessing.

## Why existing solutions fall short

- **Steam's own recommendations** only see Steam, and optimize for what Valve wants to sell.
- **Similarity tools** (SteamPeek and friends) match games to games but don't know what I've played and loved — so "similar to Hades" ignores that I finished Hades.
- **HowLongToBeat and metadata sites** inform but don't recommend.
- **Reddit's "what should I play" threads** are high-signal but slow and not personalized to my actual library.

The gap: nothing reads my full cross-platform library *and* reasons from demonstrated taste *and* factors in current prices. An LLM with those inputs is uniquely positioned to do all three in one shot.

![Unified library across Steam, Epic, and GOG](docs/screenshots/library.png)

## Bets I made

- **Reason, not match.** For a once-a-day decision, reasoning depth beats response speed. An LLM can hold "long-form narrative roguelikes with a dark tone" in its head; a similarity API can't.
- **Library, not storefront.** Users think in terms of *their games*, not *their Steam* and *their GOG*. Presenting one unified view makes the whole app feel a level smarter than a single-store tool.
- **Four explicit modes, not one catch-all.** Players ask four different questions — what to play, what to buy, what's rotting in the backlog, what's out there. Explicit modes are faster than guessing intent from a single "recommend me something" prompt.
- **Free-text mood input.** "Something short I can finish this weekend" beats filter dropdowns. The bet: users will type more useful context in a freeform field than they'd select in structured filters.
- **No persistent credentials.** Explicit "no" to storing API keys or OAuth tokens. Cost: re-login after restart. Gain: a home-server exposure leaks nothing durable.

![Streaming recommendation mid-response](docs/screenshots/streaming.png)

## What I'd cut if I shipped this publicly

- The four-mode tab switcher collapses to a single "Recommend" button with intent inferred from the mood prompt.
- The standalone Deals Library card moves to secondary nav — power-user affordance, not a primary flow.
- Model selection and the deep-thinking toggle hide behind an "Advanced" disclosure.

## What I'd add

- **Onboarding.** A guided sixty-second path from install to first recommendation. No empty-state pedagogy today.
- **Thumbs-up / thumbs-down feedback** fed into the next prompt as "avoid this kind of suggestion." Personalization that compounds.
- **Mobile view.** The dense deals table is hostile to phones — which is exactly the "on the couch, what should I play?" moment.
- **Shareable recommendation cards.** A viral loop without requiring social accounts.

![Current deals with historical-low verdicts](docs/screenshots/deals.png)

## How I'd know it's working

- **Recommendation-acceptance rate** — % of picks the user clicks through or accepts. A proxy for "did this help me decide?"
- **Time-to-decision** — from "Recommend" click to outbound click or session end. Should trend shorter over repeat use as the model internalizes taste.
- **Weekly active use** — are users coming back? If not, the taste profile isn't compounding and the value isn't sticky.

What I'd *not* optimize for: purchases driven through Sales mode. Incentive conflict — I'd rather users trust the tool than have it push them to spend.

---

*CRIT is a personal tool I use several times a week. This brief is how I'd pitch it as a real product; the engineering-side reasoning lives in [CASE_STUDY.md](CASE_STUDY.md).*
