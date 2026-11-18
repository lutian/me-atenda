"""Chat transcript: lead on the left, agent on the right; live on Leads, same view in History."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))
from chat import bubbles
import test_prepared_messages as dispatch

TRANSCRIPT = [{'role': 'assistant', 'content': 'Olá, aqui é a Bia. Posso falar com você?'},
              {'role': 'user', 'content': 'Pode falar. <b>sem html</b>'},
              {'role': 'assistant', 'content': 'Integramos cartórios. Você cuida disso?'}]


class Bubbles(unittest.TestCase):
    def test_lead_left_agent_right_in_order(self):
        self.assertEqual(bubbles(TRANSCRIPT), [
            {'side': 'right', 'who': 'Agente', 'text': TRANSCRIPT[0]['content']},
            {'side': 'left', 'who': 'Lead', 'text': TRANSCRIPT[1]['content']},
            {'side': 'right', 'who': 'Agente', 'text': TRANSCRIPT[2]['content']}])

    def test_malformed_turns_are_skipped(self):
        self.assertEqual(bubbles([{'role': 'system', 'content': 'x'}, {'role': 'user', 'content': None},
                                  'oops', {'role': 'user'}]), [])
        self.assertEqual(bubbles(None), [])


class LiveAPI(dispatch.FakeAPI):
    def __init__(self):
        super().__init__()
        self.status = {'opening': 'O', 'pitch': 'P', 'goodbye': 'G', 'ready': True}
        self.transcript = TRANSCRIPT[:1]; self.state = 'ligando'; self.reads = []
    def call(self, call_id):
        self.reads.append(call_id)
        return {'id': call_id, 'status_final': self.state, 'transcription_json': list(self.transcript)}


def chat_texts(app):
    return [(message.name, [t.value for t in message.text]) for message in app.chat_message]


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit image required')
class LiveChatUI(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        import client
        self.api = LiveAPI()
        factory = patch.object(client, 'APIClient', lambda *a: self.api)
        factory.start(); self.addCleanup(factory.stop)
        self.app = AppTest.from_file(str(FRONTEND / 'app.py'), default_timeout=5).run()

    def dial(self):
        self.app.sidebar.radio[0].set_value('Leads').run()
        self.app.selectbox(key='dispatch-lead').set_value(1)
        self.app.selectbox(key='dispatch-product').set_value(1).run()
        self.app.checkbox[0].check().run()
        next(b for b in self.app.button if b.label == 'Iniciar UMA ligação').click().run()
        self.assertFalse(self.app.exception)

    def test_dispatch_shows_conversation_and_updates_on_refresh(self):
        self.dial()
        self.assertEqual(chat_texts(self.app), [('assistant', ['Agente', TRANSCRIPT[0]['content']])])
        self.api.transcript = TRANSCRIPT
        self.app.run()
        self.assertEqual(chat_texts(self.app), [('assistant', ['Agente', TRANSCRIPT[0]['content']]),
                                                ('user', ['Lead', TRANSCRIPT[1]['content']]),
                                                ('assistant', ['Agente', TRANSCRIPT[2]['content']])])
        self.assertFalse(self.app.markdown and any('<b>' in m.value for m in self.app.markdown))
        self.assertTrue(any('Em andamento' in c.value for c in self.app.caption))

    def test_finished_call_shows_final_status(self):
        self.dial()
        self.api.state = 'sem_interesse'
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any('sem_interesse' in i.value for i in self.app.info))

    def test_backend_failure_while_polling_is_a_warning(self):
        from client import APIError
        self.dial()
        def fail(call_id): raise APIError('Backend unavailable or timed out.')
        self.api.call = fail
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any('Não foi possível atualizar' in w.value for w in self.app.warning))

    def test_no_live_panel_before_dialing(self):
        self.app.sidebar.radio[0].set_value('Leads').run()
        self.assertEqual(len(self.app.chat_message), 0)
        self.assertEqual(self.api.reads, [])


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit image required')
class HistoryChatUI(unittest.TestCase):
    def test_history_renders_the_same_chat(self):
        from streamlit.testing.v1 import AppTest
        import client
        import test_history as history
        api = history.FakeAPI()
        original = api.call
        def call(call_id):
            row = original(call_id); row['transcription_json'] = TRANSCRIPT
            return row
        api.call = call
        with patch.object(client, 'APIClient', lambda *a: api):
            app = AppTest.from_file(str(FRONTEND / 'app.py'), default_timeout=5).run()
            app.sidebar.radio[0].set_value('Histórico').run()
            app.selectbox[0].set_value(11).run()
        self.assertFalse(app.exception)
        self.assertEqual([m.name for m in app.chat_message], ['assistant', 'user', 'assistant'])
        self.assertIn('Baixar transcrição JSON', [b.label for b in app.get('download_button')])


if __name__ == '__main__': unittest.main()
