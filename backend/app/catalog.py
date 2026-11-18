"""AI model catalog: which rates apply to each option and which Settings field it drives."""
import re

KINDS = ('text', 'audio', 'embedding')
KIND_RATES = {'text': ('input_per_million', 'cached_input_per_million', 'output_per_million'),
              'embedding': ('input_per_million',),
              'audio': ('characters_per_million',)}
SETTING_OF = {'text': 'chat_model', 'audio': 'tts_model', 'embedding': 'embedding_model'}
NAME = re.compile(r'[A-Za-z0-9_.:/-]{1,120}')
RATE = re.compile(r'\d{1,18}(?:\.\d{1,18})?')
ALL_RATES = ('input_per_million', 'cached_input_per_million', 'output_per_million', 'characters_per_million')


def valid_costs(kind, costs):
    """Only the rates of this option, each a non-negative decimal string or None."""
    return set(costs) <= set(KIND_RATES[kind]) and all(
        value is None or (isinstance(value, str) and RATE.fullmatch(value)) for value in costs.values())


def full_costs(kind, costs):
    return {rate: costs.get(rate) for rate in KIND_RATES[kind]}


def model_view(row, costs, active):
    return {'id': row['id'], 'name': row['name'], 'kind': row['kind'],
            'costs': full_costs(row['kind'], costs), 'active': active}
