"""Layout preflight only; no final document file, network or user data writes."""
import unittest
from unittest.mock import patch
from app.document_export.models import ExportRequest
from app.document_export.renderer import render_export


class HeadingTests(unittest.TestCase):
    def test_group_and_metadata_keep_with_question(self):
        for mode in ('inline', 'end'):
            with self.subTest(mode=mode):
                request = ExportRequest(format='pdf', settings={'solutionMode': mode}, groups=[{
                    'order': 1, 'title': 'GROUPHEADING', 'meta': 'GROUPMETA',
                    'questions': [{'stem': 'QUESTION', 'answer': 'ANSWER'}]}])
                flows = []
                with patch('app.document_export.renderer.BaseDocTemplate.build', side_effect=lambda story: flows.extend(story)):
                    render_export(request)
                headings = [f for f in flows if getattr(f, 'text', '') == '1. GROUPHEADING']
                self.assertTrue(headings)
                self.assertTrue(all(f.getKeepWithNext() for f in headings))
                metadata = [f for f in flows if getattr(f, 'text', '') == 'GROUPMETA']
                self.assertTrue(all(f.getKeepWithNext() for f in metadata))
                # Bodies remain splittable so a long question cannot block export.
                self.assertTrue(all(not f.getKeepWithNext() for f in flows if getattr(f, 'text', '') == '1. QUESTION'))


if __name__ == '__main__':
    unittest.main()
