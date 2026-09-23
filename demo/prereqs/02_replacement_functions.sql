CREATE OR ALTER FUNCTION dbo.fn_Get_Concatenate (@Values dbo.ConcatValueList READONLY, @Delimiter NVARCHAR(10) = N',')
RETURNS NVARCHAR(MAX) AS
BEGIN
    RETURN (SELECT ISNULL(STRING_AGG(Val, @Delimiter), N'') FROM @Values);
END
GO
CREATE OR ALTER FUNCTION dbo.fn_RegexIsMatch (@input NVARCHAR(MAX), @pattern NVARCHAR(4000), @options INT)
RETURNS BIT AS
BEGIN
    RETURN CASE WHEN @input LIKE @pattern THEN 1 ELSE 0 END; -- placeholder: real logic per pattern inventory
END
GO
CREATE OR ALTER FUNCTION dbo.fn_SplitString (@s NVARCHAR(MAX), @d NCHAR(1))
RETURNS TABLE AS RETURN (SELECT value FROM STRING_SPLIT(@s, @d));
GO
