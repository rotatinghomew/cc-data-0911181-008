from pathlib import Path
import tempfile
import unittest
from app.journal import Journal
from app.storage import DataError,Store

DATA=Path(__file__).parents[1]/'data'


class BusinessChainTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(DATA)
        self.journal=Journal()

    def tearDown(self):
        self.journal.close()

    def test_business_chain(self):
        self.exercise(self.store,self.journal)

    def test_rollback_on_failure(self):
        with self.assertRaises(DataError):
            with self.journal.transaction():
                self.journal.put('test','a',{'value':1})
                raise DataError('aborted')
        self.assertEqual(self.journal.all('test'),[])

    def test_restart_retains_business_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'workflow.sqlite3'
            journal=Journal(path)
            try:
                self.exercise(self.store,journal)
                before={kind[0] for kind in journal.connection.execute('SELECT DISTINCT kind FROM documents')}
                snapshot={kind:journal.all(kind) for kind in before}
            finally:
                journal.close()
            reopened=Journal(path)
            try:
                self.assertEqual(snapshot,{kind:reopened.all(kind) for kind in before})
            finally:
                reopened.close()

    def exercise(self,store,journal):
        from app import review_dispatch,document_requests,review_feedback
        with self.assertRaises(DataError):
            review_dispatch.assign(store,journal,dict(claim='Q00',reviewer='auditor-a'))
        review_dispatch.assign(store,journal,dict(claim='Q00',reviewer='auditor-b'))
        requested=document_requests.request(store,journal,dict(claim='Q00'))
        own={row['record'] for row in store.select('presence',claim='Q00')}
        self.assertEqual(set(requested['requested_records']),own)
        result=review_feedback.resolve(store,journal,dict(claim='Q00',reviewer='auditor-b',outcome='inconclusive'))
        self.assertEqual(result['outcome'],'inconclusive')
        self.assertEqual(journal.get('audit_assignment','Q00')['status'],'reviewed')
