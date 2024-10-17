import json
import logging
import re
import urllib.parse
from src.agents.news_agent.config import Config
from src.agents.news_agent.tools import (
    clean_html,
    is_within_time_window,
    fetch_rss_feed,
)
from src.models.messages import ChatRequest
import pyshorteners

logger = logging.getLogger(__name__)


class NewsAgent:
    def __init__(self, agent_info, llm, embeddings):
        self.agent_info = agent_info
        self.llm = llm
        self.embeddings = embeddings
        self.tools_provided = self.get_tools()
        self.url_shortener = pyshorteners.Shortener()

    def get_tools(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "fetch_crypto_news",
                    "description": "Fetch and analyze cryptocurrency news for potential price impacts",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "coins": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of cryptocurrency symbols to fetch news for",
                            }
                        },
                        "required": ["coins"],
                    },
                },
            }
        ]

    def check_relevance_and_summarize(self, title, content, coin):
        logger.info(f"Checking relevance for {coin}: {title}")
        prompt = Config.RELEVANCE_PROMPT.format(coin=coin, title=title, content=content)
        result = self.llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=Config.LLM_MAX_TOKENS,
            temperature=Config.LLM_TEMPERATURE,
        )
        return result["choices"][0]["message"]["content"].strip()

    def process_rss_feed(self, feed_url, coin):
        logger.info(f"Processing RSS feed for {coin}: {feed_url}")
        feed = fetch_rss_feed(feed_url)
        results = []
        for entry in feed.entries:
            published_time = entry.get("published") or entry.get("updated")
            if is_within_time_window(published_time):
                title = clean_html(entry.title)
                content = clean_html(entry.summary)
                logger.info(f"Checking relevance for article: {title}")
                result = self.check_relevance_and_summarize(title, content, coin)
                if not result.upper().startswith("NOT RELEVANT"):
                    results.append(
                        {"Title": title, "Summary": result, "Link": entry.link}
                    )
                if len(results) >= Config.ARTICLES_PER_TOKEN:
                    break
            else:
                logger.info(
                    f"Skipping article: {entry.title} (published: {published_time})"
                )
        logger.info(f"Found {len(results)} relevant articles for {coin}")
        return results

    def fetch_crypto_news(self, coins):
        logger.info(f"Fetching news for coins: {coins}")
        all_news = []
        for coin in coins:
            logger.info(f"Processing news for {coin}")
            coin_name = Config.CRYPTO_DICT.get(coin.upper(), coin)
            google_news_url = Config.GOOGLE_NEWS_BASE_URL.format(coin_name)
            results = self.process_rss_feed(google_news_url, coin_name)
            all_news.extend(
                [
                    {"Coin": coin, **result}
                    for result in results[: Config.ARTICLES_PER_TOKEN]
                ]
            )

        logger.info(f"Total news items fetched: {len(all_news)}")
        return all_news

    def chat(self, request: ChatRequest):
        try:
            data = request.dict()
            if "prompt" in data:
                prompt = data["prompt"]
                if isinstance(prompt, dict) and "content" in prompt:
                    prompt = prompt["content"]

                # Updated coin detection logic
                coins = re.findall(
                    r"\b("
                    + "|".join(re.escape(key) for key in Config.CRYPTO_DICT.keys())
                    + r")\b",
                    prompt.upper(),
                )

                if not coins:
                    return {
                        "role": "assistant",
                        "content": "I couldn't identify any cryptocurrency symbols in your message. Please specify the cryptocurrencies you want news for.",
                        "next_turn_agent": None,
                    }

                news = self.fetch_crypto_news(coins)

                if not news:
                    return {
                        "role": "assistant",
                        "content": "No relevant news found for the specified cryptocurrencies in the last 24 hours.",
                        "next_turn_agent": None,
                    }

                response = "Here are the latest news items relevant to changes in price movement of the mentioned tokens in the last 24 hours:\n\n"
                for index, item in enumerate(news, start=1):
                    coin_name = Config.CRYPTO_DICT.get(item["Coin"], item["Coin"])
                    short_url = self.url_shortener.tinyurl.short(item["Link"])
                    response += f"{index}. ***{coin_name} News***:\n"
                    response += f"{item['Title']}\n"
                    response += f"{item['Summary']}\n"
                    response += f"Read more: {short_url}\n\n"

                return {
                    "role": "assistant",
                    "content": response,
                    "next_turn_agent": None,
                }
            else:
                return {
                    "role": "assistant",
                    "content": "Missing required parameters",
                    "next_turn_agent": None,
                }

        except Exception as e:
            logger.error(f"Error in chat method: {str(e)}", exc_info=True)
            return {
                "role": "assistant",
                "content": f"An error occurred: {str(e)}",
                "next_turn_agent": None,
            }
