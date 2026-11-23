- **Every JSON write body an agent can send now refuses a field it does not
  declare.** Twenty-five bodies in `/openapi-agent.json` still dropped an unknown
  field and went ahead without it, so `POST /projects` sent with
  `"visibility": "private"` answered 201 with a public project. They now answer
  422 `validation_error` naming the field, as the other bodies already did:
  project and space create and edit, visibility, members and statuses, saved
  filters, users (create, role, agent flag, password), webhooks, event sources,
  page comments, page moves, pages from templates and an issue's project.
  `POST /projects` now accepts `visibility` (`public` or `private`; omitted, the
  deployment default) and applies it inside the create, so a project asked for
  as private is never public and its narrowing is on the trail. `POST /spaces`
  does not: `visibility` there is refused; set it with
  `PUT /spaces/{id}/visibility`. A client that sent extra fields to these
  routes must drop them. A test walks the whole agent document and fails on a
  body that accepts unknown fields.
