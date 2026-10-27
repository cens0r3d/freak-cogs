from redbot.core.utils import get_end_user_data_statement

from .tiktokwatch import TikTokWatch

__red_end_user_data_statement__ = get_end_user_data_statement(__file__)


async def setup(bot):
    cog = TikTokWatch(bot)
    await bot.add_cog(cog)
