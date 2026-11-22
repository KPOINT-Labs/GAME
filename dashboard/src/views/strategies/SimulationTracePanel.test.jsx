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

const breakdown = (...components) => ({
  version: 1,
  components: components.map(([key, points]) => ({ key, points })),
})

const renderPanel = (trace, program = ast, result = null) =>
  render(
    <I18nextProvider i18n={i18n}>
      <SimulationTracePanel ast={program} trace={trace} breakdown={result} />
    </I18nextProvider>,
  )

describe('SimulationTracePanel - add_bonus', () => {
  it('shows each bonus and labels the assigned value as base points', () => {
    renderPanel(
      [
        { nodeId: 'r1', type: 'rule', value: true, branch: 'match' },
        { nodeId: 'b1', type: 'add_bonus', value: 5 },
        { nodeId: 'a1', type: 'assign_points', value: 10, branch: 'match' },
      ],
      ast,
      breakdown(['base', 10], ['streak', 5]),
    )

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
      breakdown(['base', 10]),
    )

    expect(screen.getByText('Grants 10 points (case: Completed)')).toBeTruthy()
    expect(screen.queryByText(/Adds bonus/)).toBeNull()
  })

  it('shows a multiplier bonus as a multiple of the base points', () => {
    const multiplier = {
      ...ast,
      rules: [
        {
          ...ast.rules[0],
          then: [
            { ...ast.rules[0].then[0], mode: 'multiplier', value: lit('bv', 2) },
            ast.rules[0].then[1],
          ],
        },
      ],
    }
    renderPanel(
      [
        { nodeId: 'r1', type: 'rule', value: true, branch: 'match' },
        { nodeId: 'b1', type: 'add_bonus', value: 2 },
        { nodeId: 'a1', type: 'assign_points', value: 10, branch: 'match' },
      ],
      multiplier,
      breakdown(['base', 10], ['streak', 10]),
    )

    expect(screen.getByText('Adds bonus streak = ×2 base points')).toBeTruthy()
    expect(screen.getByText('Grants 10 base points (case: Completed)')).toBeTruthy()
  })

  it('marks bonuses as not applied when the fallback default assigned the points', () => {
    const withDefault = {
      ...ast,
      rules: [{ ...ast.rules[0], then: [ast.rules[0].then[0]] }],
      default: { type: 'assign_points', id: 'd1', value: lit('dv', 1), case_name: 'Fallback' },
    }
    renderPanel(
      [
        { nodeId: 'r1', type: 'rule', value: true, branch: 'match' },
        { nodeId: 'b1', type: 'add_bonus', value: 5 },
        { nodeId: 'd1', type: 'assign_points', value: 1, branch: 'match' },
      ],
      withDefault,
      breakdown(['base', 1]),
    )

    expect(screen.getByText('(not applied: no rule assigned points)')).toBeTruthy()
    expect(screen.getByText('Grants 1 points (case: Fallback)')).toBeTruthy()
  })
})
