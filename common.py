RED    = '\033[31m'
GREEN  = '\033[32m'
YELLOW = '\033[33m'
BLUE   = '\033[34m'
CYAN   = '\033[36m'
WHITE  = '\033[37m'
RESET  = '\033[0m'


def timeframe_to_seconds(tf: str) -> int:
    if tf.endswith('m'):
        return int(tf[:-1]) * 60
    if tf.endswith('h'):
        return int(tf[:-1]) * 3600
    raise ValueError(f'Unsupported timeframe: {tf}')
