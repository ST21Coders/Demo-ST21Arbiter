import { describe, it, expect } from 'vitest'
import { AGENT_CATALOG, findAgent } from '../agentCatalog'

// The catalog drives Smart Rabbit's routing: every agent id is sent verbatim
// as the sendChat `target` and must match api_handler's SPECIALIST_RUNTIME_ARNS.
const EXPECTED_GROUPS = {
  it_assist_admin: ['sharepoint', 'zscaler', 'awsconfig', 'paloalto'],
  it_assist_work: ['servicenow', 'jira'],
  employee_assist: ['hr'],
  data_assist: ['structured', 'sales'],
  insurance_assist: ['claim', 'fraud'],
  oncall_assist: ['debug'],
}

describe('AGENT_CATALOG', () => {
  it('has the six assist groups with the expected agent ids', () => {
    expect(AGENT_CATALOG.map(g => g.id)).toEqual(Object.keys(EXPECTED_GROUPS))
    for (const group of AGENT_CATALOG) {
      expect(group.agents.map(a => a.id)).toEqual(EXPECTED_GROUPS[group.id])
    }
  })

  it('every group and agent carries a name and description', () => {
    for (const group of AGENT_CATALOG) {
      expect(group.name).toBeTruthy()
      expect(group.description).toBeTruthy()
      for (const agent of group.agents) {
        expect(agent.name).toBeTruthy()
        expect(agent.description).toBeTruthy()
      }
    }
  })

  it('agent ids are globally unique (they are routing targets)', () => {
    const ids = AGENT_CATALOG.flatMap(g => g.agents.map(a => a.id))
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('findAgent resolves an id to its agent + group, null for unknown', () => {
    const hit = findAgent('servicenow')
    expect(hit.agent.id).toBe('servicenow')
    expect(hit.group.id).toBe('it_assist_work')
    expect(findAgent('nope')).toBeNull()
  })
})
