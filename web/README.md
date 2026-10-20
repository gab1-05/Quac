# Quac web console

React + Vite + TypeScript + Tailwind console for the Quac platform.

* Run instructions, page-by-page functions, and the honest thin-slice scope
  notes live in the root [`README.md`](../README.md#web-console-p7-thin).
* Regenerate the API types after any contract change (API must be running):

  ```bash
  npx openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.ts
  ```

* `npm run dev` — dev server with `/v1` + `/healthz` proxied to `localhost:8000`
* `npm run build` — type-check (`tsc -b`) + production bundle (the CI `web` job)
