"""Chat view of a call transcript: lead on the left, agent on the right.

Content is model/lead text: it is always rendered with st.text, never as Markdown or HTML.
"""

SIDES = {'user': ('left', 'Lead'), 'assistant': ('right', 'Agente')}


def bubbles(transcript):
    """Valid turns in order as {'side', 'who', 'text'}; anything malformed is skipped."""
    result = []
    for turn in transcript or []:
        if not isinstance(turn, dict) or turn.get('role') not in SIDES or not isinstance(turn.get('content'), str):
            continue
        side, who = SIDES[turn['role']]
        result.append({'side': side, 'who': who, 'text': turn['content']})
    return result


def render_chat(st, transcript):
    items = bubbles(transcript)
    if not items:
        st.caption("Sem falas ainda.")
        return
    for item in items:
        left = item['side'] == 'left'
        columns = st.columns([3, 1] if left else [1, 3])
        with columns[0 if left else 1]:
            with st.chat_message('user' if left else 'assistant'):
                st.text(item['who'])
                st.text(item['text'])
