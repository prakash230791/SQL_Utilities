SET ANSI_NULLS ON;
GO
SET QUOTED_IDENTIFIER ON;
GO
CREATE FUNCTION dbo.fn_ToTitleCase (@Input NVARCHAR(MAX))
RETURNS NVARCHAR(MAX)
AS
BEGIN
    -- Native replacement for the retired CLR scalar function dbo.clr_ToTitleCase.
    DECLARE @Result NVARCHAR(MAX) = N'';
    DECLARE @Len INT = LEN(ISNULL(@Input, N''));
    DECLARE @i INT = 1;
    DECLARE @Ch NCHAR(1);
    DECLARE @StartOfWord BIT = 1;
    WHILE @i <= @Len
    BEGIN
        SET @Ch = SUBSTRING(@Input, @i, 1);
        SET @Result += CASE WHEN @StartOfWord = 1 THEN UPPER(@Ch) ELSE LOWER(@Ch) END;
        SET @StartOfWord = CASE WHEN @Ch = N' ' THEN 1 ELSE 0 END;
        SET @i += 1;
    END
    RETURN @Result;
END
GO
