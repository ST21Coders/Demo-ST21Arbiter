// Mirror LLM Control config from Infra/params/<env>.json into a Vite env file
// the local dev server reads. The deployed build gets these same VITE_* vars
// from Infra/post_deploy_ui.py (.env.production); this script is the local-dev
// equivalent so editing dev.json is reflected by `npm run dev`.
//
// Writes ui/.env.development.local (gitignored, highest priority in dev mode),
// touching only the guardrail/model keys — other VITE_* lines are preserved.
//
// Run automatically via the `predev` npm hook, or manually: npm run gen:llm-env
import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const ENV = process.env.ENVIRONMENT || 'dev'
const PARAMS_FILE = resolve(HERE, '..', '..', 'Infra', 'params', `${ENV}.json`)
const OUT_FILE = resolve(HERE, '..', '.env.development.local')

const NOVA_LITE = 'us.amazon.nova-2-lite-v1:0'

const params = Object.fromEntries(
  JSON.parse(readFileSync(PARAMS_FILE, 'utf8'))
    .filter(p => p.ParameterKey)
    .map(p => [p.ParameterKey, p.ParameterValue ?? '']),
)

const project = params.ProjectName || 'st21arbiter-poc'
const defaultModel = params.DefaultModelId || NOVA_LITE
const model = (key) => params[key] || defaultModel

const managed = {
  VITE_GUARDRAIL_NAME: `${ENV}-${project}-guardrail`,
  VITE_GUARDRAIL_ID: params.GuardrailId || '',
  VITE_GUARDRAIL_VERSION: params.GuardrailVersion || 'DRAFT',
  VITE_GUARDRAIL_VERSIONS: params.GuardrailVersions || params.GuardrailVersion || 'DRAFT',
  VITE_MASTER_MODEL_ID: model('MasterModelId'),
  VITE_SHAREPOINT_MODEL_ID: model('SharepointModelId'),
  VITE_AWSCONFIG_MODEL_ID: model('AwsConfigModelId'),
  VITE_ZSCALER_MODEL_ID: model('ZscalerModelId'),
}

// Preserve any pre-existing lines that we don't manage.
const kept = existsSync(OUT_FILE)
  ? readFileSync(OUT_FILE, 'utf8').split('\n').filter(
      line => line.trim() && !Object.keys(managed).some(k => line.startsWith(`${k}=`)),
    )
  : []

const body = [
  '# Generated from Infra/params/' + ENV + '.json by scripts/gen-llm-env.mjs — do not edit by hand.',
  ...kept,
  ...Object.entries(managed).map(([k, v]) => `${k}=${v}`),
].join('\n') + '\n'

writeFileSync(OUT_FILE, body)
console.log(`[gen-llm-env] wrote ${OUT_FILE} from ${PARAMS_FILE}`)
