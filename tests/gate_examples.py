TO_BOT_SMALL_TALK = ('morning Skopax', 'hey Skopax', 'Skopax how are you', 'Skopax you too?', 'you rock Skopax', 'nice work Skopax', 'thanks Skopax \U0001F64F',
                     'Skopax are you asleep', 'come on Skopax say something', 'Skopax you are ignoring us', 'Skopax want a coffee', 'thanks Skopax', 'good job Skopax',
                     'Skopax you there?', 'Skopax it is raining here', 'lovely weather today Skopax')
TO_BOT_ON_TOPIC = ('Skopax if this is fine shall we resolve it', 'Skopax what is this error', 'Skopax is everything fine after the deploy')
ABOUT_THE_BOT = ('is our small talk limit used up', 'Skopax how many questions do we have left this hour', 'Skopax how are the limits', 'how many asks are left this hour')
ABOUT_OUR_APPS = ('should it work like android or like ios', 'how does this screen work on ios', 'why does it look different on android',
                  'should this behave like the android app or the ios one')
ATTACHED_IMAGE = ('what is this [1 image attached]', 'why does this error show up [1 image attached]', 'what is this error [2 images attached]')
ABOUT_THE_BOTS_POST = ('the picture did not come through, send it again', 'send the screenshot again', 'send the link again', 'the screenshot did not load, send it again')
PLAYFUL_FOLLOW_UP = ('so you are saying all is well', 'so we can sleep tight', 'are you tired too')
ANSWER_TO_THE_BOT = ('no, I just came to chat', 'no, just came to say hi', 'no, I was just curious', 'me', 'you tell me', 'dunno, you?')
CHECKING_FOLLOW_UP = ('you sure about that', 'where are you looking', 'so no problem then, did you check the logs too', 'are you sure?')
FRIENDLY_WORD_TO_A_TEAMMATE = ('Skopax say bravo to Alice', 'Skopax say hi to Bob', 'Skopax say well done to Alice')
TEASING = ('Skopax has really lost it', 'Skopax is tired today too', 'Skopax is ignoring us again')
NOT_ADDRESSED = ("look at Skopax's note", 'Skopax already wrote it', 'Skopax caught this one well', 'I asked Skopax and it did not answer', 'I closed the Skopax card',
                 'let me tidy up its prompt', 'ok', '\U0001F44D', 'could have come from the local setups, yes')
OFF_TOPIC_NEVER_SMALL_TALK = ('Skopax what do you think about Dave', 'X ships so many bugs, right Skopax', 'Skopax write me a poem', 'Skopax what will the weather be tomorrow',
                              'Skopax who won the match')
TAKES_CARD = ("I'm checking it", "I'll take a look", 'on it', 'looking', "I'm looking", 'looking now', "I'm on it", "I'm handling it", "I'll sort it out")
RESOLVES_CARD = ('all good now', 'solved', 'fix deployed, it is fine now', 'checked, no problem', 'no action needed', 'false alarm', 'Bob fixed it', 'it is fixed now')
KNOWN_NOISE = ('this always happens, nightly batch', 'a known situation', 'this is normal, it is like this every day', 'known issue, expected')
MUTES_CARD = ("let's silence it for now", "let's mute it until tomorrow", "let's mute this for a day", 'mute it for today')
NO_CARD_INTENT = ('Bob is looking', 'who is looking at this?', 'anyone on it?', 'Carol is on it', 'fixed already?', "we'll look tomorrow")

GROUPS = {
    'to the bot, small talk': (TO_BOT_SMALL_TALK, {'addressed': True, 'on_topic': False, 'small_talk': True}, 'small-talk'),
    'to the bot, on topic': (TO_BOT_ON_TOPIC, {'addressed': True, 'on_topic': True, 'small_talk': False}, 'accepted'),
    'about the bot itself': (ABOUT_THE_BOT, {'addressed': True, 'on_topic': True, 'small_talk': False}, 'accepted'),
    'about our own apps and features': (ABOUT_OUR_APPS, {'addressed': True, 'on_topic': True, 'small_talk': False}, 'accepted'),
    'about an attached image': (ATTACHED_IMAGE, {'addressed': True, 'on_topic': True, 'small_talk': False}, 'accepted'),
    "about the bot's own post": (ABOUT_THE_BOTS_POST, {'addressed': True, 'on_topic': True, 'small_talk': False}, 'accepted'),
    'playful follow-up': (PLAYFUL_FOLLOW_UP, {'addressed': True, 'on_topic': False, 'small_talk': True}, 'small-talk'),
    'answer to what the bot said': (ANSWER_TO_THE_BOT, {'addressed': True, 'on_topic': False, 'small_talk': True}, 'small-talk'),
    'checking follow-up': (CHECKING_FOLLOW_UP, {'addressed': True, 'on_topic': True, 'small_talk': False}, 'accepted'),
    'a friendly word to a teammate': (FRIENDLY_WORD_TO_A_TEAMMATE, {'addressed': True, 'on_topic': False, 'small_talk': True}, 'small-talk'),
    'teasing that names the bot': (TEASING, {'addressed': True, 'on_topic': False, 'small_talk': True}, 'small-talk'),
    'not addressed': (NOT_ADDRESSED, {'addressed': False, 'on_topic': False, 'small_talk': False}, 'not-addressed'),
    'off topic, never small talk': (OFF_TOPIC_NEVER_SMALL_TALK, {'addressed': True, 'on_topic': False, 'small_talk': False}, 'off-topic'),
    'takes the card': (TAKES_CARD, {'addressed': False, 'on_topic': True, 'small_talk': False, 'card_intent': 'take'}, 'take-offered'),
    'resolves the card': (RESOLVES_CARD, {'addressed': False, 'on_topic': True, 'small_talk': False, 'card_intent': 'resolve'}, 'resolve-offered'),
    'known noise': (KNOWN_NOISE, {'addressed': False, 'on_topic': True, 'small_talk': False, 'card_intent': 'known'}, 'known-offered'),
    'mutes the card': (MUTES_CARD, {'addressed': False, 'on_topic': True, 'small_talk': False, 'card_intent': 'mute'}, 'mute-offered'),
    'no card intent': (NO_CARD_INTENT, {'addressed': False, 'on_topic': True, 'small_talk': False, 'card_intent': 'none'}, 'not-addressed'),
}
