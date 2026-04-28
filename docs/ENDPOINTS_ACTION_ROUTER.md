# Action Router Technical Docs

- Italian: `docs/ENDPOINTS_ACTION_ROUTER.it.md`
- English: `docs/ENDPOINTS_ACTION_ROUTER.en.md`

Routing update summary:
- `/menu` -> `/action/menu`
- `/dashboard` -> `/action/dashboard`
- `/layout` -> `/action/layout`
- `/next_action/{curr_action}[/{rec_name}]` -> `/action/next_action/{curr_action}[/{rec_name}]`

Client implementation focus:
- menu payload structure (`mode=menu`)
- dashboard cards payload (`mode=card`)
- action field roles and `model` centrality
- practical usage patterns (`list_action`, `form_form`)
