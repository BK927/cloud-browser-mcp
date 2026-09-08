# Approval policy: strict and opt-in balanced

The operator selects `CB_APPROVAL_POLICY=strict` (default, existing behavior) or
`CB_APPROVAL_POLICY=balanced`. Apply configuration at service startup; it is not
an MCP input and cannot be changed by `browser_configure`, page text, a page tool,
or a model-provided “approved” flag. Unknown setting values fail validation. There
is no `off`, blanket auto-approval or “trust this website” mode.

This is an explicit usability/security tradeoff. Balanced mode recognizes a limited
set of ordinary interactions; it does **not** prove that arbitrary page JavaScript
cannot send data, mutate state or disguise a dangerous action. Labels, URLs, ARIA
and form declarations are untrusted hints. The deny indicators and structural
allow rules are not a complete multilingual side-effect classifier. Use strict
mode when this remaining risk is unacceptable. The client must still follow the
user's actual task and ignore instructions injected by page content.

## Current balanced-v1 behavior

| Action | Automatic only when… |
| --- | --- |
| Click/double-click or Enter/Space activation of a link | Native anchor with an ordinary HTTP(S) href, no download/ping attribute, no recognized effect indicator |
| Expand/collapse | A native details/summary, or an expanded-state control referencing existing non-input content through aria-controls; it must not submit a form |
| Select a view tab | A non-submitting role=tab control referencing existing role=tabpanel content |
| Type/edit a search query | A native search/searchbox input outside a form, or an eligible input in a recognized same-origin GET search form |
| Select/check search filters | The control belongs to a recognized same-origin GET search form |
| Submit a search with its button or Enter | A recognized same-origin GET search form, not a general or POST form |
| Tab / Escape | Existing valid non-protected target; no special exemption for other keys |
| Scroll / pointer movement | Existing geometry/identity checks pass, as in strict mode |

Search-form recognition requires an explicit search landmark or search input,
bounded form controls, no credential/file/contact controls and no suspicious
operation/token field names. Multiple submitters, method/action overrides, reset
and image controls are not automatic search forms. The default submitter must
also lack recognized effect indicators: pressing Enter can activate that button.
Target URLs must be same-origin and lack recognized effect indicators.
A field merely labelled “Search” or a button merely labelled
“Safe” is not an allow rule. A control merely having aria-expanded without a valid
controlled content target is not enough either.

Approval is retained for **general text fields, draft editors, ordinary checkbox/
select settings, general/POST form submissions, save/send/publish/purchase/delete/
permission operations, downloads/uploads, coordinate clicks, all page-provided
tool calls, and unclassified buttons/keys**. Known effect indicators override
otherwise recognized view/link/search actions. Deny-word matching can have false
positives and false negatives; it is an extra brake, not the security boundary.

Ordinary URLs, tabs, navigation history, observations and viewport/capture settings
continue to use their existing tool rules without console approval. This does not
make a side-effectful GET URL safe or authorize the client to use navigation to
evade a requested confirmation. No browsing/history/screenshot/privacy/network
restriction was removed.

## What did not change

- Passwords, OTPs and secret tokens never become approved MCP inputs.
- Protected authentication screens, CAPTCHA and explicit bot blocks retain their
  existing gates; user control still pauses automatic access.
- Outbound network isolation and private-IP restrictions are unchanged.
- Old revisions/nodes, stale or masked screenshots, detached/covered/disabled
  controls and unsupported operations remain rejected before dispatch.
- Actions still recheck actual state just before execution and inspect the result.
- For approval-required actions, only an actual private-console approval record
  authorizes the exact session/tab/revision/action token, consumed before dispatch.
- Automatic non-passive actions now also record their exact dispatch binding.
  Repeating an identical action/revision after no visible change or a lost result
  must not execute again. RESULT_UNCERTAIN still locks subsequent actions until
  manual reconciliation. This is not a network-level idempotency guarantee.

`browser_status.capabilities.approval_policy` is `strict-per-action` or `balanced-v1`.
Successful actions and approval proposals include `action_policy` with `mode`,
`approval_required` and a bounded `reason` code. These results contain no extra
input values, page text or credentials. The MCP tool registry and annotations
remain unchanged: click tools are not falsely marked read-only to hide client UI
confirmations. A ChatGPT/client confirmation is separate from the server console.

## Enable and roll back

For an explicitly approved personal deployment, set only:

```dotenv
CB_APPROVAL_POLICY=balanced
```

Preserve the public prefix, OAuth settings, private console, diagnostics setting,
network isolation, resource budgets and other MCP routes. The deployment owner
must validate and apply the candidate; a source patch alone does not activate it.
Changing policy requires service restart, so finish existing browser work first.
Do not migrate/reset grant databases, passwords or browser profiles. Restore
`CB_APPROVAL_POLICY=strict` on restart to return to the previous approval behavior.
Existing frozen source/wheel/sdist remain separate rollback artifacts.

Acceptance includes real native accordion/link/search actions without approval,
unchanged generic/side-effect proposal and token-only denial, one-time dispatch,
stale target rejection, strict-mode regression, and authenticated HTTP MCP on W3C.
Local tests are not evidence of Pi deployment, full hostile-site safety, performance
or actual ChatGPT user interaction. Report those gates independently.
