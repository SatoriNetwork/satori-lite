''' random name generator '''

import random
from satorilib.utils.name import adjectives, animals, foods, quotes


def getRandomNoun() -> str:
    if random.randint(1, 100) > 13:
        return random.choice(animals.animals)
    else:
        return random.choice(foods.foods)


def getRandomAdjective() -> str:
    return random.choice(adjectives.adjectivesUpperCase)


def getRandomName() -> str:
    return getRandomAdjective() + ' ' + getRandomNoun()


def getRandomQuote() -> tuple[str, str]:
    return random.choice(list(quotes.quotes.items()))
