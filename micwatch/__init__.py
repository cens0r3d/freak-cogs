from .micwatch import MicWatch


async def setup(bot):
    """Load the MicWatch cog."""
    await bot.add_cog(MicWatch(bot))
