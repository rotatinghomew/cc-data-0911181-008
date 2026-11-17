from copy import deepcopy
import json
from pathlib import Path
import unittest

from app import document_requests, review_dispatch, review_feedback
from app.analysis import ROUTES
from app.evaluation import compare
from app.journal import Journal
from app.leads import DISCLAIMER, group_cap, model, plan
from app.storage import DataError, Store

DATA = Path(__file__).parents[1] / 'data'


class LeadModelTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(DATA)

    def test_groups_are_cross_school_and_explain_why(self):
        result = model(self.store)
        self.assertTrue(result['groups'])
        for group in result['groups']:
            self.assertTrue(group['cross_school'])
            self.assertGreaterEqual(len(group['claims']), 2)
            self.assertTrue(group['reasons'])
            self.assertIn('grouping_is_a_lead_not_a_finding', group['caveats'])
            self.assertNotIn('penalty', json.dumps(group))

    def test_single_shared_agent_or_venue_is_not_enough(self):
        result = model(self.store)
        groups = [group for group in result['groups']
                  if set(group['claims']) == {'Q00', 'Q04'}]
        self.assertEqual(groups, [])
        # 共用代办人和场地本身很常见，不能仅凭这些关系就分组。
        row = next(row for row in result['claims'] if row['claim'] == 'Q00')
        self.assertTrue(any('共用' in reason or '到场' in reason or '单笔' in reason
                            for reason in row['reasons']))

    def test_missing_attachment_alone_is_not_a_group(self):
        row = next(row for row in model(self.store)['claims']
                   if row['claim'] == 'Q16')
        self.assertIn('missing_attachment', row['document_issues'])
        self.assertIsNone(row['group_id'])
        self.assertIn('missing_attachment_is_a_document_gap_not_fraud', row['caveats'])

    def test_reviewed_claims_are_context_not_negative_examples(self):
        full = model(self.store)
        reviewed = {row['claim'] for row in self.store.table('reviews')}
        for group in full['groups']:
            self.assertFalse(set(group['claims']) & reviewed and not group['reasons'])
        for row in full['claims']:
            if row['claim'] in reviewed:
                self.assertEqual(row['review_state'], 'reviewed')

    def test_scope_open_excludes_reviewed_and_keeps_remaining_members(self):
        full = model(self.store)
        opened = model(self.store, scope='open')
        reviewed = {row['claim'] for row in self.store.table('reviews')}
        self.assertFalse({row['claim'] for row in opened['claims']} & reviewed)
        group = next(group for group in opened['groups'] if 'Q24' in group['claims'])
        self.assertEqual(group['claims'], ['Q03', 'Q24'])
        self.assertIn('Q03', group['open_claims'])
        self.assertIn('Q24', group['reviewed_claims'])
        self.assertEqual(group['reasons'] and '关联依据已调整' in group['reasons'][-1], True)
        self.assertIn('review_progress_does_not_close_remaining_claims', group['caveats'])

    def test_risk_does_not_use_review_outcomes(self):
        altered = Store(DATA)
        for row in altered._tables['reviews']:
            row['outcome'] = 'needs_correction' if row['outcome'] == 'supported' else 'supported'
        before = {row['claim']: row['risk'] for row in model(self.store)['claims']}
        after = {row['claim']: row['risk'] for row in model(altered)['claims']}
        self.assertEqual(before, after)

    def test_disclaimer_and_external_data_note(self):
        result = model(self.store)
        self.assertEqual(result['disclaimer'], DISCLAIMER)
        self.assertTrue(any('外部个人资料' in note for note in result['signal_notes']))

    def test_group_size_cap_records_suppressed_links(self):
        self.store._tables['policies'][0]['max_group_size'] = 2
        result = model(self.store)
        self.assertTrue(result['suppressed_links'])
        for item in result['suppressed_links']:
            self.assertEqual(item['reason'], 'group_size_cap')
            self.assertTrue(item['note'])
        for group in result['groups']:
            self.assertLessEqual(len(group['claims']), 2)

    def test_group_cap_is_bounded(self):
        self.assertEqual(group_cap({'max_group_share': 0.4}, 5), 2)
        self.assertEqual(group_cap({'max_group_share': 0}, 5), 2)  # 非法值回退默认
        self.assertEqual(group_cap({'max_group_share': 0.4}, 1), 1)


class LeadPlanTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(DATA)

    def test_daily_plan_caps_each_group(self):
        result = plan(self.store, {'budget': ['5']})
        self.assertEqual(len(result['selected']), 5)
        self.assertEqual(result['group_cap'], 2)
        counts = {}
        for row in result['selected']:
            if row['group_id']:
                counts[row['group_id']] = counts.get(row['group_id'], 0) + 1
        self.assertTrue(all(count <= result['group_cap'] for count in counts.values()))

    def test_deferred_keeps_high_risk_visible(self):
        # budget=3 时组上限为 1，第二个同组高风险班次应被明确延后而非丢弃。
        result = plan(self.store, {'budget': ['3']})
        self.assertEqual(result['group_cap'], 1)
        selected = {row['claim'] for row in result['selected']}
        deferred = {row['claim']: row for row in result['deferred']}
        capped = [row for row in deferred.values() if row['reason'] == 'group_daily_cap']
        self.assertTrue(capped)
        for claim in deferred:
            self.assertNotIn(claim, selected)
        unselected_risk = [row['risk'] for row in deferred.values()]
        selected_risk = [row['risk'] for row in result['selected']]
        self.assertGreaterEqual(max(unselected_risk), min(selected_risk))
        # 被延后的班次仍可从分析中看到完整理由。
        analysis = {row['claim']: row for row in model(self.store)['claims']}
        for row in capped:
            self.assertTrue(analysis[row['claim']]['reasons'])

    def test_plan_never_includes_reviewed(self):
        result = plan(self.store, {})
        reviewed = {row['claim'] for row in self.store.table('reviews')}
        self.assertFalse({row['claim'] for row in result['selected']} & reviewed)
        self.assertFalse({row['claim'] for row in result['deferred']} & reviewed)

    def test_plan_route_is_read_only_and_repeatable(self):
        first = ROUTES['/api/plan'](self.store, {'budget': ['4']})
        second = ROUTES['/api/plan'](self.store, {'budget': ['4']})
        self.assertEqual(first, second)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(DATA)
        self.journal = Journal()

    def tearDown(self):
        self.journal.close()

    def test_bulk_dispatch_avoids_conflicts_and_caps_groups(self):
        # auditor-a 与 S0 有利益关联，应自动落到其他审核员。
        result = review_dispatch.dispatch(self.store, self.journal, {'budget': 5})
        self.assertEqual(result['assigned_count'], 5)
        for row in result['assigned']:
            if row['school'] == 'S0':
                self.assertNotEqual(row['reviewer'], 'auditor-a')
        counts = {}
        for row in result['assigned']:
            if row['group_id']:
                counts[row['group_id']] = counts.get(row['group_id'], 0) + 1
        self.assertTrue(all(count <= result['group_cap'] for count in counts.values()))

    def test_group_conflict_on_another_member_blocks_assignment(self):
        store = Store(DATA)
        store._tables['reviewers'] = [
            {'reviewer': 'auditor-x', 'conflicts': [], 'capacity': 3,
             'conflicts_by_field': {'teacher': ['T1']}},
            {'reviewer': 'auditor-y', 'conflicts': [], 'capacity': 3},
        ]
        with self.assertRaises(DataError):
            review_dispatch.assign(store, self.journal, {'claim': 'Q08', 'reviewer': 'auditor-x'})
        self.assertEqual(review_dispatch.assign(
            store, self.journal, {'claim': 'Q08', 'reviewer': 'auditor-y'})['group_id'],
            'G5d86ff44')

    def test_direct_assign_respects_group_daily_cap(self):
        # daily_budget=2 时组上限为 1，第二笔同组派工应被拒绝。
        self.store._tables['policies'][0]['daily_budget'] = 2
        review_dispatch.assign(self.store, self.journal, {'claim': 'Q01', 'reviewer': 'auditor-b'})
        with self.assertRaises(DataError):
            review_dispatch.assign(self.store, self.journal,
                                   {'claim': 'Q22', 'reviewer': 'auditor-b'})

    def test_recusal_frees_slot_and_keeps_claim_open(self):
        result = review_dispatch.dispatch(self.store, self.journal, {'budget': 5})
        first = result['assigned'][0]
        review_dispatch.recuse(self.store, self.journal,
                               {'claim': first['claim'], 'reviewer': first['reviewer'],
                                'reason': 'related to school staff'})
        self.assertEqual(self.journal.get('audit_assignment', first['claim'])['status'], 'recused')
        with self.assertRaises(DataError):
            document_requests.request(self.store, self.journal, {'claim': first['claim']})
        reassigned = review_dispatch.assign(self.store, self.journal,
                                            {'claim': first['claim'], 'reviewer': 'auditor-b'})
        self.assertEqual(reassigned['status'], 'assigned')
        self.assertEqual(len(self.journal.all('audit_recusal')), 1)

    def test_partial_review_does_not_close_siblings(self):
        result = review_dispatch.dispatch(self.store, self.journal, {'budget': 5})
        first = result['assigned'][0]
        document_requests.request_group(self.store, self.journal, {'claim': first['claim']})
        review_feedback.resolve(self.store, self.journal,
                                {'claim': first['claim'], 'reviewer': first['reviewer'],
                                 'outcome': 'supported'})
        group = next(iter(self.journal.all('audit_assignment')))
        siblings = set(group['group_claims']) - {first['claim']}
        for claim in siblings:
            with self.assertRaises(DataError):
                # 同组其他班次没有派工，不能被连带结案。
                review_feedback.resolve(self.store, self.journal,
                                        {'claim': claim, 'reviewer': first['reviewer'],
                                         'outcome': 'supported'})
        opened = model(self.store, self.journal, 'open')
        self.assertTrue(siblings & {row['claim'] for row in opened['claims']} or not siblings)


class DocumentRequestTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(DATA)
        self.journal = Journal()
        review_dispatch.assign(self.store, self.journal, {'claim': 'Q00', 'reviewer': 'auditor-b'})

    def tearDown(self):
        self.journal.close()

    def test_multi_school_request_only_sends_own_records(self):
        result = document_requests.request_group(self.store, self.journal, {'claim': 'Q00'})
        self.assertGreaterEqual(len(result['requests']), 2)
        schools = {row['school'] for row in result['requests']}
        self.assertEqual(schools, {'S0', 'S1'})
        for row in result['requests']:
            own = {record['record']
                   for claim in row['claims']
                   for record in self.store.select('presence', claim=claim)}
            self.assertEqual(set(row['requested_records']), own)
            sent_other = set(row['requested_records']) - own
            self.assertEqual(sent_other, set())
            for summary in row['other_schools']:
                self.assertNotIn('requested_records', summary)
                self.assertNotIn('people', summary)
                self.assertNotIn('records', summary)

    def test_repeated_request_does_not_duplicate(self):
        first = document_requests.request_group(self.store, self.journal, {'claim': 'Q00'})
        second = document_requests.request_group(self.store, self.journal, {'claim': 'Q00'})
        self.assertEqual([row['request_key'] for row in first['requests']],
                         [row['request_key'] for row in second['requests']])
        self.assertEqual(len(self.journal.all('document_request')), len(first['requests']))

    def test_resolution_uses_own_school_request(self):
        grouped = document_requests.request_group(self.store, self.journal, {'claim': 'Q00'})
        own = next(row for row in grouped['requests'] if row['school'] == 'S0')
        other = next(row for row in grouped['requests'] if row['school'] == 'S1')
        result = review_feedback.resolve(self.store, self.journal,
                                         {'claim': 'Q00', 'reviewer': 'auditor-b',
                                          'outcome': 'inconclusive'})
        self.assertEqual(result['requested_records'], own['requested_records'])
        self.assertNotEqual(result['requested_records'], other['requested_records'])
        self.assertEqual(self.journal.get('document_request', own['request_key'])['status'],
                         'fulfilled')
        self.assertEqual(self.journal.get('document_request', other['request_key'])['status'],
                         'requested')

    def test_recused_assignment_needs_reassignment(self):
        review_dispatch.recuse(self.store, self.journal,
                               {'claim': 'Q00', 'reviewer': 'auditor-b', 'reason': 'x'})
        with self.assertRaises(DataError):
            document_requests.request(self.store, self.journal, {'claim': 'Q00'})


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(DATA)

    def test_unreviewed_are_unknown_not_negative(self):
        result = compare(self.store, {})
        self.assertEqual(result['selection_bias']['unreviewed_treatment'], 'unknown_not_negative')
        self.assertFalse(result['selection_bias']['population_estimate_available'])
        for side in ('old', 'new'):
            metrics = result['overall_selection'][side]
            self.assertEqual(metrics['reviewed_in_selection'] + metrics['unreviewed_in_selection'],
                             metrics['limit'])
        self.assertGreater(result['selection_bias']['review_coverage'], 0)

    def test_reviewed_stratum_comparison_is_conditional(self):
        result = compare(self.store, {'budget': ['5']})
        stratum = result['reviewed_stratum']
        self.assertEqual(stratum['old']['reviewed_population'],
                         len(self.store.table('reviews')))
        self.assertIsNotNone(stratum['old']['needs_correction_rate'])
        self.assertEqual(stratum['new']['reviewed_population'], stratum['old']['reviewed_population'])
        self.assertTrue(any('不能直接外推' in note for note in result['notes']))

    def test_feedback_source_preserves_old_selection(self):
        result = compare(self.store, {})
        self.assertTrue(result['feedback_source'])
        for row in result['feedback_source']:
            self.assertEqual(row['source'], 'historical_review')
            self.assertIsNotNone(row['old_rank'])
            self.assertIsNotNone(row['new_rank'])
            self.assertTrue(row['used_in_default_comparison'])

    def test_evaluation_does_not_change_risk(self):
        before = {row['claim']: row['risk'] for row in model(self.store)['claims']}
        compare(self.store, {})
        after = {row['claim']: row['risk'] for row in model(self.store)['claims']}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
