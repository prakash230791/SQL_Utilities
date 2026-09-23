IF TYPE_ID(N'dbo.ConcatValueList') IS NULL
    CREATE TYPE dbo.ConcatValueList AS TABLE (Val NVARCHAR(MAX) NULL);
GO
