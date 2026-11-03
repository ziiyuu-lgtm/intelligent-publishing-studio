from django.test import SimpleTestCase

from publishing.site_text_diff import MAX_TOKENS_PER_SIDE, compare_page_text


class SiteTextComparisonTests(SimpleTestCase):
    def test_exact_text_is_reconstructable_with_newlines_and_unicode(self):
        before = 'Research connects people.\n支持开放研究。 Café!'
        after = 'Research connects ideas.\n支持透明研究。 Café?'
        result = compare_page_text(before, after)
        self.assertTrue(result['highlighted'])
        self.assertEqual(''.join(p['text'] for p in result['before']), before)
        self.assertEqual(''.join(p['text'] for p in result['after']), after)
        self.assertIn('people', ''.join(p['text'] for p in result['before'] if p['changed']))
        self.assertIn('ideas', ''.join(p['text'] for p in result['after'] if p['changed']))
        self.assertIn('开放', ''.join(p['text'] for p in result['before'] if p['changed']))
        self.assertIn('透明', ''.join(p['text'] for p in result['after'] if p['changed']))

    def test_identical_text_has_no_comparison_and_empty_side_is_preserved(self):
        self.assertIsNone(compare_page_text('Same text', 'Same text'))
        result = compare_page_text('', 'New page text')
        self.assertEqual(result['before'], [])
        self.assertEqual(''.join(p['text'] for p in result['after']), 'New page text')
        self.assertTrue(all(p['changed'] for p in result['after']))

    def test_long_input_is_shown_in_full_without_highlighting(self):
        before = 'word ' * (MAX_TOKENS_PER_SIDE + 1)
        after = before + 'new'
        result = compare_page_text(before, after)
        self.assertFalse(result['highlighted'])
        self.assertEqual(result['before'][0]['text'], before)
        self.assertEqual(result['after'][0]['text'], after)

    def test_large_single_tokens_do_not_attempt_a_costly_diff(self):
        before = 'a' * 11000
        after = 'b' * 11000
        result = compare_page_text(before, after)
        self.assertFalse(result['highlighted'])
        self.assertEqual(result['before'][0]['text'], before)
        self.assertEqual(result['after'][0]['text'], after)
