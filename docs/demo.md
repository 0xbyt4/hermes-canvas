# Demo prompt

Send this prompt to Hermes in a new chat, with the Canvas pane open. Replace `/path/to/demo-shop`
with the absolute path of your demo-shop folder.

```text
Project root: /path/to/demo-shop (TypeScript, tests run with vitest).
Add payments to the shop. Plan it on the canvas first:
- a question box "Which payment provider?" with the options "Stripe" and "Adyen";
- Payment API in src/payments/**, check `npx vitest run tests/payments`, after the question;
- Checkout page in src/checkout/**, check `npx vitest run tests/checkout`, after the API;
- Refund page in src/refund/**, check `npx vitest run tests/refund`, after the API;
- Receipt email in src/receipt/**, check `npx vitest run tests/receipt.test.ts`, after the API.
  tests/receipt.test.ts comes from the shop owner: do not edit it.
Do not call any network service: the provider client is a small fake inside src/payments.
Format every price with formatMoney from src/money.ts. Change files outside the boxes only when a
check fails because of them; then fix them and say so.
Put the canvas card line in your reply and stop until I answer the question.
When I answer, build the Payment API yourself, then delegate the other boxes with delegate_task,
using the goals from canvas_next. Run each box's check command exactly as written.
```

## Story

1. The plan draws itself on the canvas, box by box.
2. The user adds a box with "+ Box" and removes one.
3. With "→ Link", the user makes the receipt email wait for the checkout page. The agent gets the
   change like any other edit.
4. The user picks the provider on the chat card. This sends the next turn.
5. Subagents build the checkout and refund pages. Each one shows as a named pointer on its box. A
   short line on its last real write or check shows for a few seconds.
6. The user leaves a note on a box while it is built. The chat card lists the note as "waiting",
   then "delivered" once the agent got it. The subagent of that box gets it too.
7. The user removes the refund page while its subagent builds it. Its next write is blocked.
8. The receipt email starts after the checkout page. `src/money.ts` has a bug (cents without a
   leading zero), so the receipt check fails red.
9. The finish gate holds the agent until the check passes, and the canvas shows a "Finish held"
   banner. The fix in `src/money.ts` shows as a drift box, and the user adds it to the plan.
10. Every box is done.

A drift box the user does not want gets "Revert" instead of "Add to plan": the agent undoes its
own change to that file and removes the box.
