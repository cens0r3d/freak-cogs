from .vcstatus import VCStatus


async def setup(bot):
    """Load the VCStatus cog."""
    await bot.add_cog(VCStatus(bot))
