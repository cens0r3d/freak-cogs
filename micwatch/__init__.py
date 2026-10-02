from redbot.core.utils import get_end_user_data_statement

from .micwatch import MicWatch

__red_end_user_data_statement__ = get_end_user_data_statement(__file__)


async def setup(bot):
    """Load the MicWatch cog."""
    await bot.add_cog(MicWatch(bot))
