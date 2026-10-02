from redbot.core.utils import get_end_user_data_statement

from .vcstatus import VCStatus

__red_end_user_data_statement__ = get_end_user_data_statement(__file__)


async def setup(bot):
    """Load the VCStatus cog."""
    await bot.add_cog(VCStatus(bot))
