from .deadbydaylight import DBD


async def setup(bot):
    await bot.add_cog(DBD(bot))
