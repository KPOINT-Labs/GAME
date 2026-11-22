// SimulationTracePanel: add_bonus statements and the base-points wording.

import React from 'react'
import { beforeAll, describe, expect, it } from 'vitest'
import { I18nextProvider } from 'react-i18next'
import { render, screen } from '@testing-library/react'

import i18n from '../../i18n'
import SimulationTracePanel from './SimulationTracePanel'

beforeAll(async () => {
  await i18n.changeLanguage('en')
})

const lit = (id, value) => ({ type: 'literal', id, value })

const ast = {
  type: 'program',
  id: 'program',
  rules: [
    {
      type: 'rule',
      id: 'r1',
      when: lit('l1', true),
      then: [
        { type: 'add_bonus', id: 'b1', key: 'streak', value: lit('bv', 5) },
        {
          type: 'assign_points',
          id: 'a1',
          value: lit('av', 10),
          case_name: 'Completed',
        },
      ],
    },
  ],
}

const renderPanel = (trace, program = ast) =>
  render(
    <I18nextProvider i18n={i18n}>
      <SimulationTracePanel ast={program} trace={trace} />
    </I18nextProvider>,
  )

describe('SimulationTracePanel - add_bonus', () => {
  it('shows each bonus and labels the assigned value as base points', () => {
    renderPanel([
      { nodeId: 'r1', type: 'rule', value: true, branch: 'match' },
      { nodeId: 'b1', type: 'add_bonus', value: 5 },
      { nodeId: 'a1', type: 'assign_points', value: 10, branch: 'match' },
    ])

    expect(screen.getByText('Adds bonus streak = 5')).toBeTruthy()
    expect(screen.getByText('Grants 10 base points (case: Completed)')).toBeTruthy()
  })

  it('keeps the plain wording when no bonus ran', () => {
    const withoutBonus = {
      ...ast,
      rules: [{ ...ast.rules[0], then: [ast.rules[0].then[1]] }],
    }
    renderPanel(
      [
        { nodeId: 'r1', type: 'rule', value: true, branch: 'match' },
        { nodeId: 'a1', type: 'assign_points', value: 10, branch: 'match' },
      ],
      withoutBonus,
    )

    expect(screen.getByText('Grants 10 points (case: Completed)')).toBeTruthy()
    expect(screen.queryByText(/Adds bonus/)).toBeNull()
  })
})
