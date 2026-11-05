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

1. The plan draws itself on the canvas.
2. The user adds a box with "+ Box" and removes one.
3. The user picks the provider on the chat card. This sends the next turn.
4. Subagents build, and their boxes light up.
5. The user leaves a note on a box while it is built.
6. The user removes the refund page during the build. Its next write is blocked.
7. `src/money.ts` has a bug (cents without a leading zero). The receipt check fails red.
8. The finish gate holds the agent until the check passes. The fix in `src/money.ts` shows as a
   drift box, and the user adds it to the plan.
9. Every box is done.
