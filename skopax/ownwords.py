KNOWN_REASONS = {'chat': 'marked as known noise from a chat answer'}
KNOWN_REASON = 'marked as known noise on the page'
BOARD_REASON = 'marked as known noise on the board'
REASONS = frozenset({KNOWN_REASON, BOARD_REASON, *KNOWN_REASONS.values()})
